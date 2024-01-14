#!/usr/bin/env python3
# -*- coding: utf-8 -*-
CONFIG = {
    'name': '@4879',
    'path': './data',
    'log': './log',
    'visual': './visual',
    'gpu_id': "0",
    'note': 'some_note',
    'model': 'MIDGN',
    'dataset_name': 'iFashion',
    'task': 'tune',
    'eval_task': 'test',

    ## optimal hyperparameters
    'lrs': [1e-3],
    'message_dropouts': [0.3],
    'node_dropouts': [0.1],
    'decays': [4e-5],

    ## hard negative sample and further train
    'sample': 'simple',
    'hard_window': [0.7, 1.0], # top 30%
    'hard_prob': [0.3, 0.3], # probability 0.8
    'conti_train': 'log/iFashion_sample/',

    ## other settings
    'epochs': 50,
    'early': 20,
    'log_interval': 20,
    'test_interval': 5,
    'retry': 1,

    ## test path
    'test':['log/iFashion'],
    'batch_size_train': 2048,
    'batch_size_test': 2048,
    'n_layers': 2,
    'corDecay': 1e-2,
    'topk_pos': 30,
    'topk_neg': 30,
}

