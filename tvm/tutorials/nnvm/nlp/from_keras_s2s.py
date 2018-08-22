"""
Compile Keras Models
=====================
**Author**: `Siju Samuel <https://siju-samuel.github.io/>`_

This article is an introductory tutorial to deploy keras models with NNVM.

For us to begin with, keras should be installed.
Tensorflow is also required since it's used as the default backend of keras.

A quick solution is to install via pip
```
pip install -U keras --user
```
```
pip install -U tensorflow --user
```
or please refer to official site
https://keras.io/#installation
"""
import nnvm
import tvm
import keras
import numpy as np
from keras.models import Model, load_model
from keras.layers import Input

def download(url, path, overwrite=False):
    import os
    if os.path.isfile(path) and not overwrite:
        print('File {} exists, skip.'.format(path))
        return
    print('Downloading from url {} to {}'.format(url, path))
    try:
        import urllib.request
        urllib.request.urlretrieve(url, path)
    except:
        import urllib
        urllib.urlretrieve(url, path)

######################################################################
# Load pretrained keras model
# ----------------------------
# We load a pretrained resnet-50 classification model provided by keras.
weights_url = ''.join(['https://github.com/dmlc/web-data/blob/master/keras/models/s2s/s2s.h5'])
weights_file = 'addition_lstm.h5'
download(weights_url, weights_file)
keras_model = load_model(weights_file)

'''
######################################################################
# Load a test image
# ------------------
# A single cat dominates the examples!
from PIL import Image
from matplotlib import pyplot as plt
from keras.applications.resnet50 import preprocess_input
img_url = 'https://github.com/dmlc/mxnet.js/blob/master/data/cat.png?raw=true'
download(img_url, 'cat.jpg')
img = Image.open('cat.jpg').resize((224, 224))
plt.imshow(img)
plt.show()
# input preprocess
data = np.array(img)[np.newaxis, :].astype('float32')
data = preprocess_input(data).transpose([0, 3, 1, 2])
print('input_1', data.shape)
'''

######################################################################
# Compile the model on NNVM
# --------------------------
# We should be familiar with the process now.

# convert the keras model(NHWC layout) to NNVM format(NCHW layout).
sym, params = nnvm.frontend.from_keras(keras_model)
# compile the model
target = 'llvm'
input_1 = np.zeros((1, 71))
input_2 = np.zeros((1, 94))

shape_dict = {'input_1': input_1.shape,
              'input_2': input_2.shape}
with nnvm.compiler.build_config(opt_level=2):
    graph, lib, params = nnvm.compiler.build(sym, target, shape_dict, params=params)

######################################################################
# Execute on TVM
# ---------------
# The process is no different from other examples.
from tvm.contrib import graph_runtime
ctx = tvm.cpu(0)
m = graph_runtime.create(graph, lib, ctx)
# set inputs
m.set_input('input_1', tvm.nd.array(data.astype('float32')))
m.set_input(**params)
# execute
m.run()
# get outputs
out_shape = (1000,)
tvm_out = m.get_output(0, tvm.nd.empty(out_shape, 'float32')).asnumpy()
top1_tvm = np.argmax(tvm_out)

#####################################################################
# Look up synset name
# -------------------
# Look up prdiction top 1 index in 1000 class synset.
synset_url = ''.join(['https://gist.githubusercontent.com/zhreshold/',
                      '4d0b62f3d01426887599d4f7ede23ee5/raw/',
                      '596b27d23537e5a1b5751d2b0481ef172f58b539/',
                      'imagenet1000_clsid_to_human.txt'])
synset_name = 'synset.txt'
download(synset_url, synset_name)
with open(synset_name) as f:
    synset = eval(f.read())
print('NNVM top-1 id: {}, class name: {}'.format(top1_tvm, synset[top1_tvm]))
# confirm correctness with keras output
keras_out = keras_resnet50.predict(data.transpose([0, 2, 3, 1]))
top1_keras = np.argmax(keras_out)
print('Keras top-1 id: {}, class name: {}'.format(top1_keras, synset[top1_keras]))
