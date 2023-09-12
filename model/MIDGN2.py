#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import torch
import torch.nn as nn
import torch.nn.functional as F
import scipy.sparse as sp
import numpy as np
from .model_base import Info, Model
import pdb
from config import CONFIG
import torch_sparse
from torch_sparse import SparseTensor
from torch_sparse.mul import mul
from torch.nn.parameter import Parameter

import pdb
def spspdot(indexA, valueA, indexB, valueB, m, k, coalesced=False):
    """Matrix product of two sparse tensors. Both input sparse matrices need to
    be coalesced (use the :obj:`coalesced` attribute to force).

    Args:
        indexA (:class:`LongTensor`): The index tensor of first sparse matrix.
        valueA (:class:`Tensor`): The value tensor of first sparse matrix.
        indexB (:class:`LongTensor`): The index tensor of second sparse matrix.
        valueB (:class:`Tensor`): The value tensor of second sparse matrix.
        m (int): The first dimension.
        k (int): The second dimension.
        coalesced (bool, optional): If set to :obj:`True`, will coalesce both
            input sparse matrices. (default: :obj:`False`)

    :rtype: (:class:`LongTensor`, :class:`Tensor`)
    """

    A = SparseTensor(row=indexA[0], col=indexA[1], value=valueA,
                     sparse_sizes=(m, k), is_sorted=not coalesced)
    B = SparseTensor(row=indexB[0], col=indexB[1], value=valueB,
                     sparse_sizes=(m, k), is_sorted=not coalesced)

    C = A*B
    row, col, value = C.coo()

    return torch.stack([row, col], dim=0), value
def graph_generating(raw_graph, row, col):
    if raw_graph.shape == (row, col):
        graph = sp.bmat([[sp.identity(raw_graph.shape[0]), raw_graph],
                             [raw_graph.T, sp.identity(raw_graph.shape[1])]])
    else:
        raise ValueError(r"raw_graph's shape is wrong")
    return graph

def laplace_transform(graph):
    rowsum_sqrt = sp.diags(1/(np.sqrt(graph.sum(axis=1).A.ravel()) + 1e-8))
    colsum_sqrt = sp.diags(1/(np.sqrt(graph.sum(axis=0).A.ravel()) + 1e-8))
    graph = rowsum_sqrt @ graph @ colsum_sqrt
    return graph

def to_tensor(graph):
    graph = graph.tocoo()
    values = graph.data
    indices = np.vstack((graph.row, graph.col))
    graph = torch.sparse.FloatTensor(torch.LongTensor(indices), torch.FloatTensor(values),
                                         torch.Size(graph.shape))
    #graph=SparseTensor(row=torch.tensor(graph.row, dtype=torch.long),col=torch.tensor(graph.col, dtype=torch.long),value=torch.tensor(values, dtype=torch.float),sparse_sizes=torch.Size(graph.shape))
    return graph




class MIDGN_Info(Info):
    def __init__(self, embedding_size, embed_L2_norm, mess_dropout, node_dropout, num_layers, act=nn.LeakyReLU()):
        super().__init__(embedding_size, embed_L2_norm)
        self.act = act
        assert 1 > mess_dropout >= 0
        self.mess_dropout = mess_dropout
        assert 1 > node_dropout >= 0
        self.node_dropout = node_dropout
        assert isinstance(num_layers, int) and num_layers > 0
        self.num_layers = num_layers


class MIDGN(Model):
    def get_infotype(self):
        return MIDGN_Info

    def __init__(self, info, dataset, raw_graph, device, pretrain=None):
        super().__init__(info, dataset, create_embeddings=True)

        self.epison = 1e-8
        self.cor_flag = 1
        self.corDecay = 1e-2
        self.n_factors = 4
        self.n_layers = 3
        self.num_layers = 2
        self.n_iterations = 2
        self.pick_level = 1e10
        self.c_temp = 0.25
        self.beta = 0.04
        self.topk_pos = 20 # topk users/bundles in contrastive loss
        self.topk_neg = 20
        self.device = device
        emb_dim = int(int(self.embedding_size) / self.n_factors)
        self.items_feature_each = nn.Parameter(
            torch.FloatTensor(self.num_items, emb_dim)).to(device)
        nn.init.xavier_normal_(self.items_feature_each)
        self.items_feature = torch.cat([self.items_feature_each for i in range(self.n_factors)], dim=1)
        assert isinstance(raw_graph, list)
        ub_graph, ui_graph, bi_graph = raw_graph
        ui_graph_coo, ub_graph_coo, bi_graph_coo = ui_graph.tocoo(), ub_graph.tocoo(), bi_graph.tocoo()
        ub_indices = torch.tensor([ub_graph_coo.row, ub_graph_coo.col], dtype=torch.long)
        ub_values = torch.ones(ub_graph_coo.data.shape, dtype=torch.float)
        bi_indices = torch.tensor([bi_graph_coo.row, bi_graph_coo.col], dtype=torch.long)
        bi_values = torch.ones(bi_graph_coo.data.shape, dtype=torch.float)
        ui_e_indices, ui_e_values = torch_sparse.spspmm(ub_indices, ub_values, bi_indices, bi_values, self.num_users,
                                                        self.num_bundles, self.num_items)

        ui_graph_e = sp.csr_matrix((np.array([1] * len(ui_e_values)), (ui_e_indices[0].numpy(), ui_e_indices[1].numpy())),
            shape=(self.num_users, self.num_items))
        ui_graph_e_coo = ui_graph_e.tocoo()
        ui_graph_e_coo = ui_graph_e.tocoo()
        # self.ui_mask = ui_graph_e[ui_graph_coo.row, ui_graph_coo.col]
        # self.ui_e_mask = ui_graph[ui_graph_e_coo.row, ui_graph_e_coo.col]
        self.bi_graph, self.ui_graph = bi_graph, ui_graph
        # ub_sparse = torch.sparse_coo_tensor(ub_indices, ub_values, ub_graph.shape)
        # bi_sparse = torch.sparse_coo_tensor(bi_indices, bi_values, bi_graph.shape)

        # user-item graph (items from bundles users had interacted with)
        # ubi_graph = self.get_ubi_non_weighted(ub_sparse, bi_sparse)
        # item-item graph each cell is the number of times item i and j appeared at a same bundle 
        # self.ii_graph = bi_sparse.T @ bi_sparse 

        if ui_graph.shape == (self.num_users, self.num_items):
            # add self-loop
            atom_graph = sp.bmat([[sp.identity(ui_graph.shape[0]), ui_graph],
                                  [ui_graph.T, sp.identity(ui_graph.shape[1])]])
        else:
            raise ValueError(r"raw_graph's shape is wrong")
        self.ui_atom_graph = to_tensor(laplace_transform(atom_graph)).to(device)
        if bi_graph.shape == (self.num_bundles, self.num_items):
            # add self-loop
            atom_graph = sp.bmat([[sp.identity(bi_graph.shape[0]), bi_graph],
                                  [bi_graph.T, sp.identity(bi_graph.shape[1])]])
        else:
            raise ValueError(r"raw_graph's shape is wrong")
        self.bi_atom_graph = to_tensor(laplace_transform(atom_graph)).to(device)
        self.dnns_atom = nn.ModuleList([nn.Linear(
            self.embedding_size, self.embedding_size) for l in range(self.num_layers)])
        
        if bi_graph.shape == (self.num_bundles, self.num_items):
            tmp = bi_graph.tocoo()
            self.bi_graph_h = list(tmp.row)
            self.bi_graph_t = list(tmp.col)
            self.bi_graph_shape = bi_graph.shape
        else:
            raise ValueError(r"raw_graph's shape is wrong")

        if ui_graph.shape == (self.num_users, self.num_items):
            # add self-loop
            tmp = ui_graph.tocoo()
            self.ui_graph_v = torch.tensor(tmp.data, dtype=torch.float).to(device)
            self.ui_graph_h = list(tmp.row)
            self.ui_graph_t = list(tmp.col)
            self.ui_graph_shape = ui_graph.shape
        else:
            raise ValueError(r"raw_graph's shape is wrong")
        
        if ub_graph.shape == (self.num_users, self.num_bundles):
            # add self-loop
            tmp = ub_graph.tocoo()
            self.ub_graph_v = torch.tensor(tmp.data, dtype=torch.float).to(device)
            self.ub_graph_h = list(tmp.row)
            self.ub_graph_t = list(tmp.col)
            self.ub_graph_shape = ub_graph.shape
        else:
            raise ValueError(r"raw_graph's shape is wrong")
        
        # if ubi_graph.shape == (self.num_users, self.num_items):
        #     tmp = ubi_graph
        #     self.ubi_graph_v = ubi_graph.values().to(device)
        #     self.ubi_graph_h = list(ubi_graph.indices()[0])
        #     self.ubi_graph_t = list(ubi_graph.indices()[1])
        #     self.ubi_graph_shape = ubi_graph.shape
        # else:
        #     raise ValueError(r"ubi graph shape is wrong")
        
        # if ii_graph.shape == (self.num_items, self.num_items):
        #     self.ii_graph_v = ii_graph.values().to(device)
        #     self.ii_graph_h = list(ii_graph.indices()[0])
        #     self.ii_graph_t = list(ii_graph.indices()[1])
        #     self.ii_graph_shape = ii_graph.shape
        # else:
        #     raise ValueError(r"ii graph shape is wrong")
        # print('finish generating bi, ui graph')

        #  deal with weights
        bi_norm = sp.diags(1 / (np.sqrt((bi_graph.multiply(bi_graph)).sum(axis=1).A.ravel()) + 1e-8)) @ bi_graph
        bb_graph = bi_norm @ bi_norm.T

        bundle_size = bi_graph.sum(axis=1) + 1e-8
        bi_graph = sp.diags(1 / bundle_size.A.ravel()) @ bi_graph

        if ub_graph.shape == (self.num_users, self.num_bundles) \
                and bb_graph.shape == (self.num_bundles, self.num_bundles):
            # add self-loop
            non_atom_graph = sp.bmat([[sp.identity(ub_graph.shape[0]), ub_graph],
                                      [ub_graph.T, bb_graph]])
        else:
            raise ValueError(r"raw_graph's shape is wrong")
        self.non_atom_graph = to_tensor(laplace_transform(non_atom_graph)).to(device)
        print('finish generating non-atom graph')

        # copy from info
        self.act = self.info.act
        self.num_layers = self.info.num_layers
        self.device = device

        #  Dropouts
        self.mess_dropout = nn.Dropout(self.info.mess_dropout, True)
        self.node_dropout = nn.Dropout(self.info.node_dropout, True)

        # Layers
        self.dnns_non_atom = nn.ModuleList([nn.Linear(
            self.embedding_size, self.embedding_size) for l in range(self.num_layers)])
        # self.dnns_non_atom2_atom=nn.Linear(self.embedding_size * 6, self.embedding_size,bias=False)
        # pretrain
        if not pretrain is None:
            self.users_feature.data = F.normalize(
                pretrain['users_feature'])
            self.items_feature.data = F.normalize(
                pretrain['items_feature'])
            self.bundles_feature.data = F.normalize(
                pretrain['bundles_feature'])
            
        # self.pos_u_ids, self.neg_u_ids = torch.arange(0, self.num_users).view(1, -1)\
        #                                 .expand(self.topk_pos + self.topk_neg, self.num_users).flatten().view(-1, 1)\
        #                                 .split([self.topk_pos * self.num_users, self.topk_neg * self.num_users])
        # self.pos_b_ids, self.neg_b_ids = torch.arange(0, self.num_bundles).view(1, -1)\
        #                                 .expand(self.topk_pos + self.topk_neg, self.num_bundles).flatten().view(-1, 1)\
        #                                 .split([self.topk_pos * self.num_bundles, self.topk_neg * self.num_bundles])
        
        # ii_dense = self.ii_graph.to_dense()        
        # self.pos_item_set = [i.to_sparse_coo().indices().view(-1, ) for i in ii_dense]
        # self.neg_item_set = [i.to_sparse_coo().indices().view(-1, ) for i in (ii_dense==0)]

        # del ii_dense
        # self.corel_items = self.cal_C_item(ii_dense)        

    def one_propagate(self, graph, A_feature, B_feature, dnns):
        # node dropout on graph
        indices = graph._indices()
        values = graph._values()
        values = self.node_dropout(values)
        graph = torch.sparse.FloatTensor(
            indices, values, size=graph.shape)
        #B_feature = torch.cat([B_feature for i in range(4)], 1)
        # propagate
        features = torch.cat((A_feature, B_feature), 0)
        all_features = [features]
        for i in range(self.num_layers):
            features = self.mess_dropout(self.act(
                dnns[i](torch.matmul(graph, features))))
            all_features.append(F.normalize(features))

        # all_features = torch.cat(all_features, 1)
        all_features = torch.stack(all_features, dim=1)
        all_features = torch.mean(all_features, dim=1, keepdims=False)
        A_feature, B_feature = torch.split(
            all_features, (A_feature.shape[0], B_feature.shape[0]), 0)
        return A_feature, B_feature

    def ub_propagate(self, graph, A_feature, B_feature):
        # node dropout on graph
        indices = graph._indices()
        values = graph._values()
        values = self.node_dropout(values)
        graph = torch.sparse.FloatTensor(
            indices, values, size=graph.shape)

        # propagate
        features = torch.cat((A_feature, B_feature), 0)

        all_features = torch.matmul(graph, features)
        # all_features=torch.mean(all_features,dim=1,keepdims=False)
        A_feature, B_feature = torch.split(
            all_features, (A_feature.shape[0], B_feature.shape[0]), 0)
        return A_feature, B_feature

    def propagate(self):

        # bi_value_sparse,ui_value_sparse=[],[]
        # ub_indices = torch.tensor(np.array([self.ub_graph_h, self.ub_graph_t]), dtype=torch.long).to(self.device)
        # bi_indices = torch.tensor(np.array([self.bi_graph_h, self.bi_graph_t]), dtype=torch.long).to(self.device)
        # ui_indices = torch.tensor(np.array([self.ui_graph_h, self.ui_graph_t]), dtype=torch.long).to(self.device)

        atom_bundles_feature, atom_item_feature, self.bi_avalues = self._create_star_routing_embed_with_p(self.bi_graph_h,
                                                                                                     self.bi_graph_t,
                                                                                                     self.bundles_feature,
                                                                                                     self.items_feature,
                                                                                                     self.num_bundles,
                                                                                                     self.num_items,
                                                                                                     self.bi_graph_shape,
                                                                                                     n_factors=1,
                                                                                                     pick_=False)

        atom_user_feature, atom_item_feature2, self.ui_avalues = self._create_star_routing_embed_with_p(self.ui_graph_h,
                                                                                                   self.ui_graph_t,
                                                                                                   self.users_feature,
                                                                                                   self.items_feature,
                                                                                                   self.num_users,
                                                                                                   self.num_items,
                                                                                                   self.ui_graph_shape,
                                                                                                   n_factors=self.n_factors,
                                                                                                   pick_=False)

        ui_avalues_e_list = []
        ui_avalues_list = []

        non_atom_users_feature, non_atom_bundles_feature = self.ub_propagate(
            self.non_atom_graph, atom_user_feature, atom_bundles_feature)

        users_feature = [atom_user_feature, non_atom_users_feature]
        bundles_feature = [atom_bundles_feature, non_atom_bundles_feature]

        return users_feature, bundles_feature, atom_bundles_feature, atom_item_feature, atom_user_feature

    def predict(self, users_feature, bundles_feature):
        users_feature_atom, users_feature_non_atom = users_feature  # batch_n_f
        bundles_feature_atom, bundles_feature_non_atom = bundles_feature  # batch_n_f
        pred = torch.sum(users_feature_atom * bundles_feature_atom, 2) \
               + torch.sum(users_feature_non_atom * bundles_feature_non_atom, 2)
        return pred

    def forward(self, users, bundles):
        users_feature, bundles_feature, atom_bundles_feature, atom_item_feature, atom_user_feature = self.propagate()
        users_embedding = [i[users].expand(- 1, bundles.shape[1], -1) for i in
                           users_feature]  # u_f --> batch_f --> batch_n_f
        bundles_embedding = [i[bundles] for i in bundles_feature]  # b_f --> batch_n_f
        pred = self.predict(users_embedding, bundles_embedding)
        loss = self.regularize(users_embedding, bundles_embedding)
        items = torch.tensor([np.random.choice(self.bi_graph[i].indices) for i in bundles.cpu()[:, 0]]).type(
            torch.int64).to(self.device)
        l_cor = ( self.contrastive_loss(users_feature[0], users_feature[1], self.topk_pos, self.topk_neg) \
                + self.contrastive_loss(bundles_feature[0], bundles_feature[1], self.topk_pos, self.topk_neg, usr=False)) / 2
        loss = loss
        ii_loss = 0
        ii_loss = self.cal_bpr_item(self.ii_graph, atom_item_feature)
        print(ii_loss)
        return pred, loss, l_cor * self.beta + ii_loss # side loss

    def regularize(self, users_feature, bundles_feature):
        users_feature_atom, users_feature_non_atom = users_feature  # batch_n_f
        bundles_feature_atom, bundles_feature_non_atom = bundles_feature  # batch_n_f
        loss = self.embed_L2_norm * \
               ((users_feature_atom ** 2).sum() + (bundles_feature_atom ** 2).sum() + \
                (users_feature_non_atom ** 2).sum() + (bundles_feature_non_atom ** 2).sum())
        return loss

    def evaluate(self, propagate_result, users):
        '''
        just for testing, compute scores of all bundles for `users` by `propagate_result`
        '''
        users_feature, bundles_feature, _, _, _ = propagate_result
        users_feature_atom, users_feature_non_atom = [i[users] for i in users_feature]  # batch_f
        bundles_feature_atom, bundles_feature_non_atom = bundles_feature  # b_f
        scores = torch.mm(users_feature_atom, bundles_feature_atom.t()) \
                 + torch.mm(users_feature_non_atom, bundles_feature_non_atom.t())  # batch_b
        return scores

    def _create_star_routing_embed_with_p(self, all_h_list, all_t_list, featureA, featureB, numA, numB, A_inshape, n_factors=4,
                                          pick_=False):
        '''
        pick_ : True, the model would narrow the weight of the least important factor down to 1/args.pick_scale.
        pick_ : False, do nothing.
        '''
        '''
        need parameter:
        n_factor

        user_embedding --> bundle_feature
        item_embedding --> item_feature
        self.A_in_shape
        A:all_h_list, all_t_list

        '''
        p_test = False
        p_train = False
        A_indices = torch.tensor([all_h_list, all_t_list], dtype=torch.long).to(self.device)
        D_indices_col = torch.tensor([list(range(numA)), list(range(numA))]).to(self.device)
        D_indices_row = torch.tensor([list(range(numB)), list(range(numB))]).to(self.device)
        A_values = torch.ones(n_factors, len(all_h_list)).to(self.device)
        
        all_A_embeddings = [featureA]
        all_B_embeddings = [featureB]
        factor_num = [n_factors, n_factors, n_factors, n_factors, n_factors, n_factors]
        iter_num = [self.n_iterations, self.n_iterations, self.n_iterations, self.n_iterations, self.n_iterations,
                    self.n_iterations]
        for k in range(0, self.n_layers):
            # prepare the output embedding list
            # .... layer_embeddings stores a (n_factors)-len list of outputs derived from the last routing iterations.
            n_factors_l = factor_num[k]
            n_iterations_l = iter_num[k]
            A_layer_embeddings = []
            B_layer_embeddings = []

            # split the input embedding table
            # .... ego_layer_embeddings is a (n_factors)-len list of embeddings [n_users+n_items, embed_size/n_factors]
            ego_layer_A_embeddings = torch.split(featureA, int(featureA.shape[1] / n_factors_l), 1)
            ego_layer_B_embeddings = torch.split(featureB, int(featureB.shape[1] / n_factors_l), 1)
            # ego_layer_embeddings=[torch.cat([A, featureB], 0) for A in ego_layer_A_embeddings]
            # perform routing mechanism
            for t in range(0, n_iterations_l):
                A_iter_embeddings = []
                B_iter_embeddings = []
                A_iter_values = []

                # split the adjacency values & get three lists of [n_users+n_items, n_users+n_items] sparse tensors
                # .... A_factors is a (n_factors)-len list, each of which is an adjacency matrix
                # .... D_col_factors is a (n_factors)-len list, each of which is a degree matrix w.r.t. columns
                # .... D_row_factors is a (n_factors)-len list, each of which is a degree matrix w.r.t. rows
                if t == n_iterations_l - 1:
                    p_test = pick_
                    p_train = False

                A_factors, A_factors_t, D_col_factors, D_row_factors = self._convert_A_values_to_A_factors_with_P(
                    n_factors_l,
                    A_values,
                    all_h_list,
                    all_t_list,
                    numA,
                    numB,
                    A_inshape,
                    pick=p_train)
                for i in range(0, n_factors_l):
                    
                    A_factor_embeddings = torch_sparse.spmm(D_indices_row, D_row_factors[i], A_inshape[1], A_inshape[1],
                                                            ego_layer_B_embeddings[i])
                    A_factor_embeddings = torch_sparse.spmm(A_indices, A_factors[i], A_inshape[0], A_inshape[1],
                                                            A_factor_embeddings)  # torch.sparse.mm(A_factors[i], factor_embeddings)

                    A_factor_embeddings = torch_sparse.spmm(D_indices_col, D_col_factors[i], A_inshape[0], A_inshape[0],
                                                            A_factor_embeddings)
                    A_iter_embedding = ego_layer_A_embeddings[i] + A_factor_embeddings

                    B_factor_embeddings = torch_sparse.spmm(D_indices_col, D_col_factors[i], A_inshape[0], A_inshape[0],
                                                            ego_layer_A_embeddings[i])
                    B_factor_embeddings = torch_sparse.spmm(A_indices[[1, 0]], A_factors_t[i], A_inshape[1],
                                                            A_inshape[0],
                                                            B_factor_embeddings)  # torch.sparse.mm(A_factors[i], factor_embeddings)

                    B_factor_embeddings = torch_sparse.spmm(D_indices_row, D_row_factors[i], A_inshape[1], A_inshape[1],
                                                            B_factor_embeddings)
                    B_iter_embedding = ego_layer_B_embeddings[i] + B_factor_embeddings
                    # A_iter_embedding,B_iter_embedding=torch.split(factor_embeddings, [numA, numB], 0)
                    A_iter_embeddings.append(A_iter_embedding)
                    B_iter_embeddings.append(B_iter_embedding)

                    if t == n_iterations_l - 1:
                        A_layer_embeddings = A_iter_embeddings
                        B_layer_embeddings = B_iter_embeddings
                        # get the factor-wise embeddings
                    # .... head_factor_embeddings is a dense tensor with the size of [all_h_list, embed_size/n_factors]
                    # .... analogous to tail_factor_embeddings
                    head_factor_embedings = A_iter_embedding[all_h_list]
                    tail_factor_embedings = ego_layer_B_embeddings[i][all_t_list]

                    # .... constrain the vector length
                    # .... make the following attentive weights within the range of (0,1)
                    head_factor_embedings = F.normalize(head_factor_embedings, dim=1)
                    tail_factor_embedings = F.normalize(tail_factor_embedings, dim=1)

                    # get the attentive weights
                    # .... A_factor_values is a dense tensor with the size of [all_h_list,1]
                    A_factor_values = torch.sum(torch.mul(head_factor_embedings, F.tanh(tail_factor_embedings)), axis=1)

                    # update the attentive weights
                    A_iter_values.append(A_factor_values)

                # pack (n_factors) adjacency values into one [n_factors, all_h_list] tensor
                A_iter_values = torch.stack(A_iter_values, 0)
                # add all layer-wise attentive weights up.
                A_values = A_values + A_iter_values

            # sum messages of neighbors, [n_users+n_items, embed_size]
            # side_embeddings = torch.cat(layer_embeddings, 1)

            # ego_embeddings = side_embeddings
            # concatenate outputs of all layers
            featureA = torch.cat(A_layer_embeddings, 1)
            featureB = torch.cat(B_layer_embeddings, 1)
            all_A_embeddings = all_A_embeddings + [featureA]
            all_B_embeddings = all_B_embeddings + [featureB]
        #all_A_embeddings = torch.cat(all_A_embeddings, 1)
        #all_B_embeddings = torch.cat(all_B_embeddings, 1)
        all_A_embeddings = torch.stack(all_A_embeddings, 1)
        all_A_embeddings = torch.mean(all_A_embeddings, dim=1, keepdims=False)
        all_B_embeddings = torch.stack(all_B_embeddings, 1)
        all_B_embeddings = torch.mean(all_B_embeddings, dim=1, keepdims=False)

        return all_A_embeddings, all_B_embeddings, A_values

    def _convert_A_values_to_A_factors_with_P(self, f_num, A_factor_values, all_h_list, all_t_list, numA, numB,
                                              A_inshape, pick=False):
        A_factors = []
        A_factors_t = []
        D_col_factors = []
        D_row_factors = []
        all_h_list = torch.tensor(all_h_list, dtype=torch.long).to(self.device)
        all_t_list = torch.tensor(all_t_list, dtype=torch.long).to(self.device)
        # get the indices of adjacency matrix

        A_indices = torch.stack([all_h_list, all_t_list], dim=0)
        # print(A_indices.shape)
        # apply factor-aware softmax function over the values of adjacency matrix
        # ....A_factor_values is [n_factors, all_h_list]
        if pick:
            A_factor_scores = F.softmax(A_factor_values, 0)
            min_A = torch.min(A_factor_scores, 0)
            index = A_factor_scores > (min_A + 0.0000001)
            index = index.type(torch.float32) * (
                    self.pick_level - 1.0) + 1.0  # adjust the weight of the minimum factor to 1/self.pick_level

            A_factor_scores = A_factor_scores * index
            A_factor_scores = A_factor_scores / torch.sum(A_factor_scores, 0)
        else:
            A_factor_scores = F.softmax(A_factor_values, 0)

        for i in range(0, f_num):
            # in the i-th factor, couple the adjcency values with the adjacency indices
            # .... A i-tensor is a sparse tensor with size of [n_users+n_items,n_users+n_items]
            A_i_scores = A_factor_scores[i]
            # A_i_tensor = torch.sparse_coo_tensor(A_indices, A_i_scores, A_inshape).to(self.device)
            A_i_tensor = SparseTensor(row=all_h_list, col=all_t_list, value=A_i_scores,
                                      sparse_sizes=(A_inshape[0], A_inshape[1]))
            D_i_col_scores = 1 / (torch.sqrt(A_i_tensor.sum(dim=1)) + 1e-10)
            D_i_row_scores = 1 / (torch.sqrt(A_i_tensor.sum(dim=0)) + 1e-10)
            _, A_i_scores_t = torch_sparse.transpose(A_indices, A_i_scores, A_inshape[0], A_inshape[1])
            A_factors.append(A_i_scores.to(self.device))
            A_factors_t.append(A_i_scores_t.to(self.device))
            D_col_factors.append(D_i_col_scores)
            D_row_factors.append(D_i_row_scores)

        # return a (n_factors)-length list of laplacian matrix
        return A_factors, A_factors_t, D_col_factors, D_row_factors
    
    def contrastive_loss(self, eck, vck, topk_pos, topk_neg, usr=True, threshold=5e-1):
        '''
        calculate for all users/bundles
        eck: users/bundles rep bf graph [n, embed_dim]
        vck: users/bundles rep af graph [n, embed_dim]
        threshold => sim_value > threshold belong to pos_set
        '''
        eck = F.normalize(eck, p=2, dim=1)
        vck = F.normalize(vck, p=2, dim=1)

        sim_mat = torch.matmul(eck, vck.T)
        pos_set = torch.topk(sim_mat, k=topk_pos, dim=1)
        neg_set = torch.topk(sim_mat, k=topk_neg, dim=1, largest=False)
        
        # contain overlap pairs
        pos_thres_mask = pos_set.values > threshold
        neg_thres_mask = neg_set.values < threshold

        pos_score = torch.sum(torch.exp(pos_set.values * pos_thres_mask), dim=1)
        neg_score = torch.sum(torch.exp(neg_set.values * neg_thres_mask), dim=1)

        '''
        eliminate overlap pairs in pos/neg sets
        '''
        # pos_ids = torch.cat(pos_set.indices.split(1, dim=1), dim=0)
        # neg_ids = torch.cat(neg_set.indices.split(1, dim=1), dim=0)

        # if usr:
        #     pos_pairs = torch.cat([self.pos_u_ids, pos_ids],dim=1).sort(dim=1).values.unique(dim=0)
        #     neg_pairs = torch.cat([self.neg_u_ids, neg_ids],dim=1).sort(dim=1).values.unique(dim=0)
        # else:
        #     pos_pairs = torch.cat([self.pos_b_ids, pos_ids],dim=1).sort(dim=1).values.unique(dim=0)
        #     neg_pairs = torch.cat([self.neg_b_ids, neg_ids],dim=1).sort(dim=1).values.unique(dim=0)

        # pos_idx, neg_idx = pos_pairs.T, neg_pairs.T
        # pos_mask_val, neg_mask_val = torch.ones(pos_idx.shape[1]), torch.ones(neg_idx.shape[1])
        # pos_mask, neg_mask = torch.sparse_coo_tensor(pos_idx, pos_mask_val, sim_mat.shape).to_dense(), \
        #                      torch.sparse_coo_tensor(neg_idx, neg_mask_val, sim_mat.shape).to_dense()
        
        # overlap_pos_neg = pos_mask * neg_mask

        # pos_sim = sim_mat * (pos_mask - overlap_pos_neg)
        # neg_sim = sim_mat * (neg_mask - overlap_pos_neg)

        # pos_score = torch.sum(torch.exp(pos_sim), dim=1)
        # neg_score = torch.sum(torch.exp(neg_sim), dim=1)
        c_loss = -torch.mean(torch.log(pos_score / neg_score))

        return c_loss

    # def get_ubi_weighted(self, ub, bi):
    #     '''
    #     ub : user-bunlde coo-graph
    #     bi : bundle-item coo-graph

    #     return: ubi user-item coo-graph through bundle 
    #     each cell [i,j] is the time user i interact with item j
    #     '''
    #     ubi = ub @ bi
    #     return ubi

    # def get_ubi_non_weighted(self, ub, bi):
    #     '''
    #     ub : user-bunlde coo-graph
    #     bi : bundle-item coo-graph

    #     return: ubi user-item coo-graph through bundle 
    #     each cell [i,j] == 0 or 1
    #     '''
    #     temp = ub @ bi
    #     idx = temp.indices()
    #     val = torch.ones_like(temp.values())
    #     ubi = torch.sparse_coo_tensor(indices=idx, values=val, size=temp.shape)
    #     return ubi
       
    # def load_ii_pairs(self, batch_size, ii_index):
    #     '''
    #     load item-item pair for BPR loss training
    #     params:
    #     ii_index: [ids,
    #                positive_ids]

    #     return:[ids, 
    #             positive_ids, 
    #             negative_ids]
    #     '''
    #     # TODO: construct pos_set in __init__()
    #     pos_item_set = None
    #     neg_ids = []
    #     id_set = torch.randint(0, ii_index.shape[1], (batch_size, ))
    #     batch_idx = ii_index.T[id_set].T.tolist()
        
    #     neg_ids = []
    #     for i in batch_idx[0]:
    #         if self.neg_item_set[i].shape == (1, 0):
    #             neg_id = 0
    #         else:
    #             neg_id = np.random.choice(self.neg_item_set[i])
    #         neg_ids.append(neg_id)

    #     batch_idx.append(neg_ids)
    #     return batch_idx
    
    # def cal_bpr_item(self, ii_graph, item_feat, mode='mean'):
    #     '''
    #     calculate BPR loss for item-item
    #     '''
    #     cur_id, pos_id, neg_id = self.load_ii_pairs(4096, ii_graph.indices())
    #     cur_feat = item_feat[cur_id]
    #     pos_feat = item_feat[pos_id]
    #     neg_feat = item_feat[neg_id]

    #     pos_score = cur_feat @ pos_feat.T
    #     neg_score = cur_feat @ neg_feat.T

    #     loss_bpr = -torch.log(torch.sigmoid(pos_score - neg_score))

    #     if mode=='mean':
    #         loss_bpr = torch.mean(loss_bpr)
    #     elif mode=='sum':
    #         loss_bpr = torch.sum(loss_bpr)
    #     else:
    #         raise ValueError(r"invalid mode i-i bpr")   
    #     return loss_bpr
    
    # def cal_C_item(self, ii_mat):
    #     val = torch.sum(ii_mat, dim=1).view(-1, )
    #     idx = [list(range(0, ii_mat.shape[0])),
    #            list(range(0, ii_mat.shape[0]))] # iden matrix
    #     Deg_mat = torch.sparse_coo_tensor(idx, val, ii_mat.shape)
    #     normD = 1/torch.sqrt(Deg_mat)
    #     return normD @ ii_mat @ normD