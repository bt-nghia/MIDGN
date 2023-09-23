import pandas as pd

t = pd.read_csv('user_bundle_train.txt', sep='\t', names=['a', 'b'])
print(t['a'].nunique())