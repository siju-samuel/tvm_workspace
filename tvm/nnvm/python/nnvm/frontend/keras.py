# pylint: disable=invalid-name, import-self
"""Keras frontend."""
from __future__ import absolute_import as _abs
import sys
import numpy as np
import tvm
from .. import symbol as _sym
from .common import SymbolTable

__all__ = ['from_keras']


def _check_data_format(keras_layer):
    if hasattr(keras_layer, ('data_format')):
        if keras_layer.data_format != 'channels_last':
            raise ValueError("Keras frontend currently supports data_format = channels_last only.")


def _get_pad_pair(input1d, kernel1d, stride1d):
    out1d = (input1d + stride1d - 1) // stride1d
    pad = np.maximum((out1d - 1) * stride1d + kernel1d - input1d, 0)
    pad_before = pad // 2
    pad_after = pad - pad_before
    return [pad_before, pad_after]

def _get_elu(insym, alpha):
    """ A helper method for elu.
    """
    return -alpha * _sym.relu(1 - _sym.exp(insym)) + _sym.relu(insym)

def _convert_activation(insym, keras_layer, _):
    if isinstance(keras_layer, str):
        act_type = keras_layer
    else:
        if sys.version_info.major < 3:
            act_type = keras_layer.activation.func_name
        else:
            act_type = keras_layer.activation.__name__
    if act_type == 'linear':
        if isinstance(keras_layer, str):
            return insym
        alpha = keras_layer.alpha if hasattr(keras_layer, "alpha") else 1
        beta = keras_layer.beta if hasattr(keras_layer, "beta") else 0
        return _sym.__add_scalar__(_sym.__mul_scalar__(insym, \
            scalar=alpha), scalar=beta)
    elif act_type == 'softmax':
        return _sym.softmax(insym, axis=1)
    elif act_type == 'sigmoid':
        return _sym.sigmoid(insym)
    elif act_type == 'tanh':
        return _sym.tanh(insym)
    elif act_type == 'relu':
        return _sym.relu(insym)
    elif act_type == 'softplus':
        return _sym.log(_sym.__add_scalar__(_sym.exp(insym), scalar=1))
    elif act_type == 'elu':
        alpha = keras_layer.alpha if hasattr(keras_layer, "alpha") else 1
        return _get_elu(insym, alpha)
    elif act_type == 'selu':
        # Alpha, Gamma values, obtained from  https://arxiv.org/abs/1706.02515
        alpha = keras_layer.alpha if hasattr(keras_layer, "alpha") else 1.6732
        gamma = keras_layer.gamma if hasattr(keras_layer, "gamma") else 1.0507
        return gamma * _get_elu(insym, alpha)
    elif act_type == 'relu6':
        return _sym.clip(insym, a_min=0, a_max=6)
    elif act_type == 'softsign':
        return insym / (1 + (_sym.relu(insym) + _sym.relu(_sym.negative(insym))))
    elif act_type == 'hard_sigmoid':
        transformX = (0.2 * insym) + 0.5
        return _sym.clip(transformX, a_min=0, a_max=1)
    else:
        raise TypeError("Unsupported activation type : {}".format(act_type))


def _convert_advanced_activation(insym, keras_layer, symtab):
    act_type = type(keras_layer).__name__
    if act_type == 'ReLU':
        return _sym.relu(insym)
    elif act_type == 'LeakyReLU':
        return _sym.leaky_relu(insym, alpha=keras_layer.alpha)
    elif act_type == 'ELU':
        alpha = keras_layer.alpha if hasattr(keras_layer, "alpha") else 1
        return _get_elu(insym, alpha)
    elif act_type == 'PReLU':
        assert hasattr(keras_layer, "alpha"), \
            "alpha required for PReLU."
        _check_data_format(keras_layer)
        size = len(keras_layer.alpha.shape)
        return -symtab.new_const(keras_layer.get_weights()[0] \
                                 .transpose(np.roll(range(size), 1))) \
                                 * _sym.relu(-insym) + _sym.relu(insym)
    elif act_type == 'ThresholdedReLU':
        theta = keras_layer.theta if hasattr(keras_layer, "theta") else 1.0
        theta_tensor = _sym.full_like(insym[0], fill_value=float(theta))
        return _sym.elemwise_mul(insym[0], _sym.greater(insym[0], theta_tensor, out_type="float32"))
    else:
        raise TypeError("Unsupported advanced activation type : {}".format(act_type))


def _convert_merge(insym, keras_layer, _):
    merge_type = type(keras_layer).__name__
    ret = insym[0]
    for i in range(1, len(insym)):
        if merge_type == 'Add':
            ret = _sym.elemwise_add(ret, insym[i])
        elif merge_type == 'Subtract':
            ret = _sym.elemwise_sub(ret, insym[i])
        elif merge_type == 'Multiply':
            ret = _sym.elemwise_mul(ret, insym[i])
        elif merge_type == 'Average':
            raise NotImplementedError('Average merge not implemented')
        elif merge_type == 'Maximum':
            raise NotImplementedError('Maximum merge not implemented')
        else:
            raise TypeError("Unsupported merge type : {}".format(merge_type))
    return ret


def _convert_dense(insym, keras_layer, symtab):
    weightList = keras_layer.get_weights()
    weight = symtab.new_const(weightList[0].transpose([1, 0]))
    params = {'weight':weight, 'use_bias':False, 'units':weightList[0].shape[1]}
    if keras_layer.use_bias:
        params['use_bias'] = True
        params['bias'] = symtab.new_const(weightList[1])
    out = _sym.dense(data=insym, **params)
    # defuse activation
    if sys.version_info.major < 3:
        act_type = keras_layer.activation.func_name
    else:
        act_type = keras_layer.activation.__name__
    if act_type != 'linear':
        out = _convert_activation(out, act_type, symtab)
    return out


def _convert_convolution(insym, keras_layer, symtab):
    _check_data_format(keras_layer)
    is_deconv = type(keras_layer).__name__ == 'Conv2DTranspose'
    is_depthconv = type(keras_layer).__name__ == 'DepthwiseConv2D'
    weightList = keras_layer.get_weights()
    if is_deconv:
        kernel_h, kernel_w, n_filters, in_channels = weightList[0].shape
        weight = weightList[0].transpose([3, 2, 0, 1])
    elif is_depthconv:
        kernel_h, kernel_w, in_channels, depth_mult = weightList[0].shape
        weight = weightList[0].transpose([2, 3, 0, 1])
    else:
        kernel_h, kernel_w, in_channels, n_filters = weightList[0].shape
        weight = weightList[0].transpose([3, 2, 0, 1])
    dilation = [1, 1]
    if isinstance(keras_layer.dilation_rate, (list, tuple)):
        dilation = [keras_layer.dilation_rate[0], keras_layer.dilation_rate[1]]
    else:
        dilation = [keras_layer.dilation_rate, keras_layer.dilation_rate]
    kernel_h = (kernel_h - 1) * dilation[0] + 1
    kernel_w = (kernel_w - 1) * dilation[1] + 1
    stride_h, stride_w = keras_layer.strides
    params = {'weight': symtab.new_const(weight),
              'kernel_size': [kernel_h, kernel_w],
              'strides': [stride_h, stride_w],
              'dilation': dilation,
              'padding': [0, 0],
              'use_bias': False}
    if is_depthconv:
        params['channels'] = in_channels * depth_mult
        params['groups'] = in_channels
    else:
        params['channels'] = n_filters
    if keras_layer.use_bias:
        params['use_bias'] = True
        params['bias'] = symtab.new_const(weightList[1])
    if keras_layer.padding == 'valid':
        pass
    # we insert a separate pad operator
    elif keras_layer.padding == 'same':
        in_h = keras_layer.input_shape[1]
        in_w = keras_layer.input_shape[2]
        pad_t, pad_b = _get_pad_pair(in_h, kernel_h, stride_h)
        pad_l, pad_r = _get_pad_pair(in_w, kernel_w, stride_w)
        insym = _sym.pad(data=insym, pad_width=((0, 0), (0, 0), (pad_t, pad_b), (pad_l, pad_r)))
    else:
        raise TypeError("Unsupported padding type : {}".format(keras_layer.padding))
    if is_deconv:
        out = _sym.conv2d_transpose(data=insym, **params)
    else:
        out = _sym.conv2d(data=insym, **params)
    # defuse activation
    if sys.version_info.major < 3:
        act_type = keras_layer.activation.func_name
    else:
        act_type = keras_layer.activation.__name__
    if act_type != 'linear':
        out = _convert_activation(out, act_type, symtab)
    return out


def _convert_separable_convolution(insym, keras_layer, symtab):
    _check_data_format(keras_layer)
    weightList = keras_layer.get_weights()
    # depthwise conv
    kernel_h, kernel_w, in_channels, depth_mult = weightList[0].shape
    stride_h, stride_w = keras_layer.strides
    weight0 = weightList[0].transpose([2, 3, 0, 1])
    params0 = {'weight': symtab.new_const(weight0),
               'channels': in_channels * depth_mult,
               'groups': in_channels,
               'kernel_size': [kernel_h, kernel_w],
               'strides': [stride_h, stride_w],
               'dilation': [1, 1],
               'padding': [0, 0],
               'use_bias': False}
    if keras_layer.padding == 'valid':
        pass
    # we insert a separate pad operator
    elif keras_layer.padding == 'same':
        in_h = keras_layer.input_shape[1]
        in_w = keras_layer.input_shape[2]
        pad_t, pad_b = _get_pad_pair(in_h, kernel_h, stride_h)
        pad_l, pad_r = _get_pad_pair(in_w, kernel_w, stride_w)
        insym = _sym.pad(data=insym, pad_width=(
            (0, 0), (0, 0), (pad_t, pad_b), (pad_l, pad_r)))
    else:
        raise TypeError("Unsupported padding type : {}".format(keras_layer.padding))
    depthconv = _sym.conv2d(data=insym, **params0)
    # pointwise conv
    weight1 = weightList[1].transpose([3, 2, 0, 1])
    params1 = {'weight': symtab.new_const(weight1),
               'channels': weight1.shape[0],
               'groups': 1,
               'kernel_size': [1, 1],
               'strides': [1, 1],
               'dilation': [1, 1],
               'use_bias': False}
    if keras_layer.use_bias:
        params1['use_bias'] = True
        params1['bias'] = symtab.new_const(weightList[2])
    out = _sym.conv2d(data=depthconv, **params1)
    # defuse activation
    if sys.version_info.major < 3:
        act_type = keras_layer.activation.func_name
    else:
        act_type = keras_layer.activation.__name__
    if act_type != 'linear':
        out = _convert_activation(out, act_type, symtab)
    return out


def _convert_flatten(insym, keras_layer, _):
    _check_data_format(keras_layer)
    # NCHW -> NHWC so that dense can be correctly converted
    insym = _sym.transpose(insym, axes=[0, 2, 3, 1])
    return _sym.flatten(insym)


def _convert_pooling(insym, keras_layer, symtab):
    _check_data_format(keras_layer)
    pool_type = type(keras_layer).__name__
    # global pool in keras = global pool + flatten in nnvm
    if pool_type == 'GlobalMaxPooling2D':
        return _convert_flatten(_sym.global_max_pool2d(insym), keras_layer, symtab)
    elif pool_type == 'GlobalAveragePooling2D':
        return _convert_flatten(_sym.global_avg_pool2d(insym), keras_layer, symtab)
    else:
        pool_h, pool_w = keras_layer.pool_size
        stride_h, stride_w = keras_layer.strides
        params = {'pool_size': [pool_h, pool_w],
                  'strides': [stride_h, stride_w],
                  'padding': [0, 0]}
        if keras_layer.padding == 'valid':
            pass
        # we insert a separate pad operator
        elif keras_layer.padding == 'same':
            in_h = keras_layer.input_shape[1]
            in_w = keras_layer.input_shape[2]
            pad_t, pad_b = _get_pad_pair(in_h, pool_h, stride_h)
            pad_l, pad_r = _get_pad_pair(in_w, pool_w, stride_w)
            insym = _sym.pad(data=insym, pad_width=(
                (0, 0), (0, 0), (pad_t, pad_b), (pad_l, pad_r)))
        else:
            raise TypeError("Unsupported padding type : {}".format(keras_layer.padding))
        if pool_type == 'MaxPooling2D':
            return _sym.max_pool2d(insym, **params)
        elif pool_type == 'AveragePooling2D':
            # TODO: in keras, padded zeros are not calculated
            return _sym.avg_pool2d(insym, **params)
        else:
            raise TypeError("Unsupported pooling type : {}".format(keras_layer))


def _convert_upsample(insym, keras_layer, _):
    _check_data_format(keras_layer)
    upsample_type = type(keras_layer).__name__
    if upsample_type == "UpSampling1D":
        h = keras_layer.size
        params = {'scale': h}
    elif upsample_type == "UpSampling2D":
        h, w = keras_layer.size
        if h != w:
            raise TypeError("Unsupported upsampling type with different axes size : {}"
                            .format(keras_layer.size))
        params = {'scale': h}
    elif upsample_type == "UpSampling3D":
        h, w, d = keras_layer.size
        if h != w or w != d:
            raise TypeError("Unsupported upsampling type with different axes size : {}"
                            .format(keras_layer.size))
        params = {'scale': h}
    else:
        raise TypeError("Unsupported upsampling type : {}".format(upsample_type))
    return _sym.upsampling(insym, **params)


def _convert_batchnorm(insym, keras_layer, symtab):
    params = {'scale': False,
              'center': False,
              'epsilon': keras_layer.epsilon}
    idx = 0
    if keras_layer.scale:
        params['scale'] = True
        gamma = keras_layer.get_weights()[idx]
        params['gamma'] = symtab.new_const(gamma)
        idx += 1
    if keras_layer.center:
        params['center'] = True
        beta = keras_layer.get_weights()[idx]
        params['beta'] = symtab.new_const(beta)
        idx += 1
    moving_mean = keras_layer.get_weights()[idx]
    moving_var = keras_layer.get_weights()[idx + 1]
    params['moving_mean'] = symtab.new_const(moving_mean)
    params['moving_var'] = symtab.new_const(moving_var)
    return _sym.batch_norm(data=insym, **params)


def _convert_padding(insym, keras_layer, _):
    _check_data_format(keras_layer)
    padding_type = type(keras_layer).__name__
    padding = keras_layer.padding
    top = left = bottom = right = 0
    if padding_type == 'ZeroPadding2D':
        if isinstance(padding, int):
            top = left = bottom = right = padding
        elif isinstance(padding, tuple):
            if isinstance(padding[0], int):
                top, left = padding
                bottom, right = padding
            elif isinstance(padding[0], tuple):
                top, bottom = padding[0]
                left, right = padding[1]
            else:
                raise ValueError("Unrecognized padding option: {}".format(str(padding)))
        else:
            raise ValueError("Unrecognized padding option: {}".format(str(padding)))
    elif padding_type == 'ZeroPadding1D':
        raise NotImplementedError("ZeroPadding1D not implemented")
    else:
        raise ValueError("Unrecognized padding type: {}".format(padding_type))
    return _sym.pad(data=insym, pad_width=((0, 0), (0, 0), (top, bottom), (left, right)))


def _convert_concat(insym, keras_layer, _):
    _check_data_format(keras_layer)
    if not isinstance(insym, list):
        insym = [insym]
    return _sym.concatenate(*insym, axis=1)


def _convert_reshape(insym, keras_layer, _):
    _check_data_format(keras_layer)
    ch = keras_layer.input_shape[-1]
    assert ch == keras_layer.target_shape[-1], \
        "Only supports last dimension in target shape being equal to " \
        "the channel number of input tensor."
    shape = (-1, ch) + keras_layer.target_shape[:-1]
    return _sym.reshape(insym, shape=shape)

_state_ctr = {}
_state_ctr['lstm_c'] = 0
_state_ctr['lstm_h'] = 0

def _new_state_sym(name, init=None):
    """Returs a symbol for state"""
    sym_name = name + "_state%d" % _state_ctr[name]
    _state_ctr[name] += 1
    return _sym.Variable(name=sym_name, init=init)

def _get_state_buffer(init_size, name):
    """Get the state buffer for rnn."""
    buffer = np.zeros((1, init_size), 'float32')
    return _new_state_sym(name, init=buffer)

def _convert_lstm(insym, keras_layer, symtab):
    _check_data_format(keras_layer)
    #print(" _convert_lstm insym = ", insym)
    #print(" _convert_lstm keras_layer", keras_layer)
    #print(" _convert_lstm symtab", symtab)
    print(" input_shape = ", keras_layer.input_shape, "Len =", len(keras_layer.input_shape))
    print(" output_shape = ", keras_layer.output_shape, "Len =", len(keras_layer.output_shape))
    #print(" state units = ", keras_layer.units)
    if not isinstance(insym, list):
        #print(" First layer")
        params = {}
        c_sym = _get_state_buffer(keras_layer.units, "lstm_c")
        h_sym = _get_state_buffer(keras_layer.units, "lstm_h")
        params['lstm_c'] = c_sym
        params['lstm_h'] = h_sym
        #print("finish LSTM layer 1")
        insym =  [insym, c_sym, h_sym]

    input_shapes = keras_layer.input_shape
    if not isinstance(input_shapes, list):
        input_shapes = [input_shapes]

    in_data = insym[0]
    in_state_c = insym[1]
    in_state_h = insym[2]

    weightList = keras_layer.get_weights()
    in_weight = symtab.new_const(weightList[0].transpose([1, 0]))
    in_bias = symtab.new_const(weightList[2])
    forget_bias = 0.0#symtab.new_const(weightList[1])

    input_shape = (1, input_shapes[0][-1])
    weight_shape = weightList[0].shape


    batch_size, input_size = input_shape[0], input_shape[1]
    num_hidden_layers = weight_shape[1]
    num_hidden = keras_layer.units

    print("")
    print("batch_size=", batch_size)
    print("input_size=", input_size)

    print("input_shape=", input_shape)
    print("weight_shape=", weight_shape)
    print("forget_bias=", forget_bias)
    print("batch_size=", batch_size)
    print("input_size=", input_size)
    print("num_hidden_layers=", num_hidden_layers)
    print("num_hidden=", num_hidden)

    in_data = _sym.reshape(in_data,
                           shape=(batch_size, input_size))
    #ixh = _sym.concatenate(*[in_data, in_state_h], axis=1)
    #in_weight = _sym.transpose(in_weight)

    gates = _sym.dense(in_data, in_weight, in_bias, use_bias=True, units=num_hidden_layers)

    gate_list = _sym.split(gates, indices_or_sections=4, axis=1)
    in_gate = _sym.sigmoid(gate_list[0])
    in_transform = _sym.tanh(gate_list[1])
    forget_gate = _sym.sigmoid(gate_list[2]) #+ forget_bias
    out_gate = _sym.sigmoid(gate_list[3])

    next_c = _sym.broadcast_add(_sym.broadcast_mul(forget_gate, in_state_c),
                                _sym.broadcast_mul(in_gate, in_transform))
    next_h = out_gate * _sym.tanh(next_c)

    #out_state = _sym.concatenate(*[next_c, next_h])
    #out_state = _sym.reshape(out_state,
    #                         shape=(2, batch_size, num_hidden))

    #print("finish LSTM layer")
    return [next_h, next_c, next_h]

def _convert_repeat_vector(insym, keras_layer, symtab):
    print("_convert_repeat_vector keras_layer.n =" ,keras_layer.n)
    print("_convert_repeat_vector input_shape = ", keras_layer.input_shape, "Len =", len(keras_layer.input_shape))
    print("_convert_repeat_vector output_shape = ", keras_layer.output_shape, "Len =", len(keras_layer.output_shape))
    return insym


def _convert_time_distributed(insym, keras_layer, symtab):
    #print("keras_layer.n =" ,keras_layer.n)
    print("_convert_time_distributed input_shape = ", keras_layer.input_shape, "Len =", len(keras_layer.input_shape))
    print("_convert_time_distributed output_shape = ", keras_layer.output_shape, "Len =", len(keras_layer.output_shape))
    return insym

def _default_skip(insym, keras_layer, _): # pylint: disable=unused-argument
    """Layers that can be skipped because they are train time only."""
    return insym


_convert_map = {
    'Dense'                    : _convert_dense,
    'Activation'               : _convert_activation,
    'ReLU'                     : _convert_advanced_activation,
    'LeakyReLU'                : _convert_advanced_activation,
    'PReLU'                    : _convert_advanced_activation,
    'ELU'                      : _convert_advanced_activation,
    'ThresholdedReLU'          : _convert_advanced_activation,

    'AveragePooling2D'         : _convert_pooling,
    'MaxPooling2D'             : _convert_pooling,
    'GlobalAveragePooling2D'   : _convert_pooling,
    'GlobalMaxPooling2D'       : _convert_pooling,
    'Conv2D'                   : _convert_convolution,
    'Conv2DTranspose'          : _convert_convolution,
    'DepthwiseConv2D'          : _convert_convolution,
    'SeparableConv2D'          : _convert_separable_convolution,

    'Flatten'                  : _convert_flatten,
    'Reshape'                  : _convert_reshape,
    'Concatenate'              : _convert_concat,
    'BatchNormalization'       : _convert_batchnorm,

    'Add'                      : _convert_merge,
    'Subtract'                 : _convert_merge,
    'Multiply'                 : _convert_merge,
    'ZeroPadding2D'            : _convert_padding,
    'UpSampling2D'             : _convert_upsample,

    # 'ZeroPadding1D'          : _convert_padding,
    # 'AveragePooling1D'       : _convert_pooling,
    # 'MaxPooling1D'           : _convert_pooling,
    # 'GlobalAveragePooling1D' : _convert_pooling,
    # 'GlobalMaxPooling1D'     : _convert_pooling,
    # 'Cropping1D'             : _convert_cropping,
    # 'Cropping2D'             : _convert_cropping,
    # 'UpSampling1D'           : _convert_upsample,
    # 'UpSampling3D'           : _convert_upsample,
    # 'Conv1D'                 : _convert_convolution1d,

    # 'GRU'                    : _convert_gru,
    'LSTM'                     : _convert_lstm,
    # 'SimpleRNN'              : _convert_simple_rnn,
    # 'Bidirectional'          : _convert_bidirectional,
    'TimeDistributed'          : _convert_time_distributed,

    # 'Average'                : _convert_merge,
    # 'Maximum'                : _convert_merge,
    # 'Dot'                    : _convert_merge,
    # 'Permute'                : _convert_permute,
    # 'Embedding'              : _convert_embedding,
    'RepeatVector'             : _convert_repeat_vector,

    'InputLayer'               : _default_skip,
    'Dropout'                  : _default_skip,
    'SpatialDropout2D'         : _default_skip,
    'SpatialDropout1D'         : _default_skip,
}


def _check_unsupported_layers(model):
    for layer in model.layers:
        if type(layer).__name__ not in _convert_map:
            raise ValueError("Keras layer {} not supported.".format(type(layer).__name__))

def _as_list(arr):
    """Force being a list, ignore if already is."""
    if isinstance(arr, list):
        return arr
    return [arr]

def keras_op_to_nnvm(insym, keras_layer, outname, symtab):
    """Convert keras layer to nnvm symbol, and update symtab.

    Parameters
    ----------
    insym : nnvm.symbol.Symbol or a list of it
        The input nnvm symbol(s)

    keras_layer : keras.layers
        The keras layer to be converted

    outname : str
        Name of the output nnvm symbol

    symtab : nnvm.frontend.common.SymbolTable
        The global symbol table to be updated
    """
    if type(keras_layer).__name__ not in _convert_map:
        raise NotImplementedError("{} is not supported".format((type(keras_layer).__name__)))
    ret = _convert_map[type(keras_layer).__name__](insym, keras_layer, symtab)
    symtab.set_var(outname, ret)

def keras_op_to_nnvm2(insym, keras_layer, outname, symtab):
    """Convert keras layer to nnvm symbol, and update symtab.

    Parameters
    ----------
    insym : nnvm.symbol.Symbol or a list of it
        The input nnvm symbol(s)

    keras_layer : keras.layers
        The keras layer to be converted

    outname : str
        Name of the output nnvm symbol

    symtab : nnvm.frontend.common.SymbolTable
        The global symbol table to be updated
    """
    if type(keras_layer).__name__ not in _convert_map:
        raise NotImplementedError("{} is not supported".format((type(keras_layer).__name__)))
    sym = _convert_map[type(keras_layer).__name__](insym, keras_layer, symtab)
    sym = _as_list(sym)
    out_tensors = len(sym)
    for tensor_idx in range(out_tensors):
            name = outname + ':' + str(tensor_idx)
            print("outname=", outname, "name =", name, "sym=", sym[tensor_idx])
            symtab.set_var(name, sym[tensor_idx])

def from_keras(model):
    """Convert keras model to NNVM format.

    Parameters
    ----------
    model : keras.engine.training.Model
        The keras model to be converted

    Returns
    -------
    sym : nnvm.Symbol
        Compatible nnvm symbol

    params : dict of str to tvm.NDArray
        The parameter dict to be used by nnvm
    """
    try:
        import keras
    except ImportError:
        raise ImportError('Keras must be installed')
    print(model.to_json(indent=4))
    assert isinstance(model, keras.engine.training.Model)
    if keras.backend.backend() != 'tensorflow':
        raise ValueError("Keras frontend currently supports tensorflow backend only.")
    if keras.backend.image_data_format() != 'channels_last':
        raise ValueError("Keras frontend currently supports data_format = channels_last only.")
    _check_unsupported_layers(model)

    symtab = SymbolTable()
    for keras_layer in model.layers:
        print("isinstance(keras_layer)=", type(keras_layer))
        if isinstance(keras_layer, keras.engine.InputLayer):
            symtab.get_var(keras_layer.name, must_contain=False)
        else:
            inbound_nodes = keras_layer.inbound_nodes if hasattr(keras_layer, 'inbound_nodes') \
                       else keras_layer._inbound_nodes if hasattr(keras_layer, '_inbound_nodes') \
                       else None
            if inbound_nodes is None:
                raise TypeError("Unknown layer type or unsupported Keras version : {}"
                                .format(keras_layer))
            for my_idx, node in enumerate(inbound_nodes):
                print("my_idx", my_idx, "inbound_nodes=", node)
                insym = []

                # Since Keras allows creating multiple layers from the same name instance,
                # we append node index to the symbol name to make it unique.
                # The one exception is InputLayer.  Changing input variable names after conversion
                # would confuse users, so we should keep them as far as possible.  Fortunately,
                # they are named uniquely to input_1, input_2, input_3 ... by default.
                for pred_idx, pred in zip(node.node_indices, node.inbound_layers):
                    print("pred_idx", pred_idx, "pred.name=", pred.name)
                    if isinstance(pred, keras.engine.InputLayer):
                        _sym = symtab.get_var(pred.name, must_contain=True)
                    else:
                        _sym = symtab.get_var(pred.name + ':' + str(pred_idx), must_contain=True)
                    insym.append(_sym)

                if len(insym) == 1:
                    insym = insym[0]
                keras_op_to_nnvm(insym, keras_layer, keras_layer.name + ':' + str(my_idx), symtab)

    outsym = symtab.get_var(model._output_layers[0].name + ':0')
    tvmparams = {k:tvm.nd.array(np.array(v, dtype=np.float32)) for k, v in symtab.params.items()}
    #print("tvmparams = ", tvmparams)
    #print("outsym = ", outsym.debug_str())
    return outsym, tvmparams

def from_keras2(model):
    """Convert keras model to NNVM format.

    Parameters
    ----------
    model : keras.engine.training.Model
        The keras model to be converted

    Returns
    -------
    sym : nnvm.Symbol
        Compatible nnvm symbol

    params : dict of str to tvm.NDArray
        The parameter dict to be used by nnvm
    """
    try:
        import keras
    except ImportError:
        raise ImportError('Keras must be installed')
    print(model.to_json(indent=4))
    assert isinstance(model, keras.engine.training.Model)
    if keras.backend.backend() != 'tensorflow':
        raise ValueError("Keras frontend currently supports tensorflow backend only.")
    if keras.backend.image_data_format() != 'channels_last':
        raise ValueError("Keras frontend currently supports data_format = channels_last only.")
    _check_unsupported_layers(model)

    symtab = SymbolTable()
    for keras_layer in model.layers:
        if isinstance(keras_layer, keras.engine.InputLayer):
            symtab.get_var(keras_layer.name, must_contain=False)
        else:
            inbound_nodes = keras_layer.inbound_nodes if hasattr(keras_layer, 'inbound_nodes') \
                       else keras_layer._inbound_nodes if hasattr(keras_layer, '_inbound_nodes') \
                       else None
            if inbound_nodes is None:
                raise TypeError("Unknown layer type or unsupported Keras version : {}"
                                .format(keras_layer))
            #print("inbound_nodes", inbound_nodes)
            for my_idx, node in enumerate(inbound_nodes):
                insym = []
                print("my_idx=", my_idx)
                # Since Keras allows creating multiple layers from the same name instance,
                # we append node index to the symbol name to make it unique.
                # The one exception is InputLayer.  Changing input variable names after conversion
                # would confuse users, so we should keep them as far as possible.  Fortunately,
                # they are named uniquely to input_1, input_2, input_3 ... by default.
                #print("node, my_idx", node , my_idx)
                prev_out_idx = 0
                #for pred_idx, out_idx, pred in zip(node.node_indices, node.tensor_indices, node.inbound_layers):
                for idx, pred in enumerate(node.inbound_layers):
                    pred = node.inbound_layers[idx]
                    pred_idx = node.node_indices[idx]
                    t_idx = node.tensor_indices[idx]

                    print("pred =", pred, "pred_idx=", pred_idx,  "pred_name=" ,pred.name)
                    if isinstance(pred, keras.engine.InputLayer):
                        _sym = symtab.get_var(pred.name, must_contain=True)
                        print("****keras.engine.InputLayer****")
                    else:
                        pred_name = pred.name + ':' + str(pred_idx)  + ':' + str(t_idx)
                        _sym = symtab.get_var(pred_name, must_contain=True)
                        print("Input Sym name=", pred_name, " sym =", _sym)
                    insym.append(_sym)

                if len(insym) == 1:
                    insym = insym[0]
                keras_op_to_nnvm(insym, keras_layer, keras_layer.name + ':' + str(my_idx), symtab)
                #print("")
                print("")
            #break
    #print("out_name=", model._output_layers[0].name)
    outsym = symtab.get_var(model._output_layers[0].name + ':0:0')
    #outsym = symtab.get_var('lstm_1:0')
    tvmparams = {k:tvm.nd.array(np.array(v, dtype=np.float32)) for k, v in symtab.params.items()}
    #print("tvmparams = ", tvmparams)
    #print("outsym = ", outsym.debug_str())
    return outsym, tvmparams

