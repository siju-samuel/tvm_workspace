"""Graph defenition class which is used to exchange the nodes information between tvm and CLI."""
from __future__ import absolute_import as _abs
import json

class GraphDef(object):
    """The class which is used to exhange the nodes inforamtion between TVM and CLI.
       This class contains the list of nodes."""
    def __init__(self, ctx, json_nodes):
        self._node = []
        for node in json_nodes['nodes']:
            self._node.append(Node(ctx, node))

    @property
    def node(self):
        return self._node

def _parse_graph(graph_json):
    """Parse and extract the NNVM graph.

    Parameters
    ----------
    graph_json : str or graph class
        The graph to be deployed in json format output by nnvm graph.

    Returns
    -------
    nodes_list : list
        List of all the nodes presented in the graph

    heads_list : list
        List of all output nodes presented in the graph

    shapes_list: list
        List of shape of each nodes presented in the graph

    dltype_list: list
        List of data type of each nodes presented in the graph
    """
    json_obj = json.loads(graph_json)
    nodes_list = json_obj['nodes']
    dltype_list = json_obj['attrs']['dltype']
    shapes_list = json_obj['attrs']['shape']
    heads_list = json_obj['heads']
    return nodes_list, dltype_list, shapes_list, heads_list

def _get_graph_json(nodes_list, dltype_list, shapes_list):
    """Create a list of nodes with name, shape and data type.

    Parameters
    ----------
    nodes_list : List
        List of nodes in the graph

    dltype_list : List
        List of datatypes of each node

    shapes_list : List
        List of shape of each node

    Returns
    -------
    p_graph : json format
        json formatted NNVM graph contain list of each node'sW name, shape and type.
    """

    p_graph = {}
    p_graph['nodes'] = []
    nodes_len = len(nodes_list)
    for i in range(nodes_len):
        node = nodes_list[i]
        input_list = []
        for input_node in node['inputs']:
            input_list.append(nodes_list[input_node[0]]['name'])
        node['inputs'] = input_list
        dltype = str("type: " + dltype_list[1][i])
        if 'attrs' not in node:
            node['attrs'] = {}
            node['op'] = "param"
        else:
            node['op'] = node['attrs']['func_name']
        node['name'] = node['name'].replace("/", "_")
        node['attrs'].update({"T": dltype})
        node['shape'] = shapes_list[1][i]
        p_graph['nodes'].append(node)
    return p_graph

def prepare_graph(graph):
    nodes_list, dltype_list, shapes_list, heads_list = _parse_graph(graph)
    p_graph = _get_graph_json(nodes_list, dltype_list, shapes_list)
    outputs = []
    for output in heads_list:
        outputs.append(nodes_list[output[0]]['name'])
    return p_graph, len(nodes_list), outputs

class Node(object):
    """The class which is used to store a node inforamtion.
       This class contains the node information like name, ops, context,
       inputs and other attributes.
       Both the arguments and operation is represented in the same node"""
    def __init__(self, ctx, node):

        name = node['name']
        op = node['op']
        device = "/ctx:" + ctx
        input_lst = []
        attr = {}
        if 'inputs' in node:
            input_lst = node['inputs']
        if 'attrs' in node:
            attr = node['attrs']

        self._name = name
        self._op = op
        self._device = device
        self._input = input_lst
        self._attr = attr

    @property
    def device(self):
        """Returns the device context"""
        return self._device

    @property
    def attr(self):
        """Returns the attributes of a node"""
        return self._attr

    @property
    def name(self):
        """Returns the name of a node"""
        return self._name

    @property
    def op(self):
        """Returns the optpe of a node"""
        return self._op

    @property
    def input(self):
        """Returns the inputs of an node which is not an argument"""
        return self._input
