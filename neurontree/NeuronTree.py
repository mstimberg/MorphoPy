import copy
import sys
import matplotlib
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd

from os import makedirs
from os.path import exists
from scipy.io import savemat
from scipy.interpolate import interp1d
from scipy import stats
from shapely.geometry import MultiLineString, LineString, Point
from itertools import combinations
from sklearn.decomposition import PCA, FastICA
from neurontree.utils import angle_between, get_rotation_matrix, rotationMatrixToEulerAngles

sys.setrecursionlimit(100000)
matplotlib.rcParams.update({'font.size': 14})


class NeuronTree:

    # resample nodes along the edges of _G in distance d, return array of 3D positions
    def resample_nodes(self, d=1):
        P = []
        for (u, v, edata) in self._G.edges(data=True):
            if self._nxversion == 2:
                # changed for version 2.x of networkX
                n1 = self._G.nodes[u]
                n2 = self._G.nodes[v]
            else:
                n1 = self._G.node[u]
                n2 = self._G.node[v]
            
            e = edata['euclidean_dist']
            m = n2['pos'] - n1['pos']
            m /= np.linalg.norm(m)

            g = lambda x: x * m + n1['pos']

            for a in range(1, int(e / d)):
                P.append(g(a * d))

        P += list(self.get_node_attributes('pos').values())
        return np.array(P)

    # creates a networkX Tree out of a swc file.
    # scaling denotes the conversion factor needed to convert the units given in swc file to microns
    # soma_rad denotes the radius of the soma given in microns
    def __init__(self, swc=None, scaling=1., node_data=[], edge_data=[], graph=None, nxversion=1):
        """
        Creates a NeuronTree object that contains a networkx.DiGraph with node attributes 'pos' [x,y,z],
        'type' [1:soma,2:axon,3: dendrite], 'radius' and edge attributes 'euclidean_dist' and 'path_length'.
        It can be created from an swc file (as pandas.DataFrame or as numpy,ndarray) from a list of nodes and
        edges or from a given networkx.DiGraph.
        :param swc: pandas.DataFrame or numpy.ndarray containing the data of an swc file.
        :param scaling: Scaling of the coordinates in swc file to microns. Sometimes the coordinates are given in voxels
        or other units.
        :param soma_rad: radius of the soma in microns.
        :param node_data: list of nodes with node id and dictionary of node attributes
        :param edge_data: list of edges with dicionary of edge attributes
        :param graph: networkx.DiGraph. It is assumed that the graph contains the required labels.
        """
        # set version of networkX
        self._nxversion = nxversion
        # initialize tree DIRECTED
        if graph:
            G = graph
        else:
            G = nx.DiGraph()

            if swc is not None:
                node_keys = ['pos', 'type', 'radius']
                
                if type(swc) == pd.DataFrame:
                    
                    if(swc.size == 0):
                        raise ValueError('No points found in swc file!')
                        
                    # sort out node data
                    n = swc['n'].values # get node ids
                    pos = np.array([swc['x'].values, swc['y'].values, swc['z'].values]).T / scaling
                    radius = swc['radius'].values / scaling
                    t = swc['type'].values
                    pid = swc['parent'].values

                elif type(swc) == np.ndarray:
                    n = swc['n']
                    pos = np.array([swc['x'], swc['y'], swc['z']]).T / scaling
                    t = swc['type']
                    radius = swc['radius'] / scaling
                    pid = swc['parent']

                else:
                    raise ValueError('Type of swc representation unknown!')

                # create node data
                t[pid == -1] = 1
                # create a list of nodes of the form [(node_id, {'pos': [x,y,z], 'type': t, 'radius' : r}),...]
                node_data = list(zip(n,
                                     [dict(zip(node_keys, [pos[ix], t[ix], radius[ix]])) for ix in range(pos.shape[0])]))

                # create edge data

                parent_idx = np.array([np.where(n == k)[0][0] if k != -1 else -1 for k in pid])

                # calculate euclidean distance between end points of edge
                ec = np.sqrt(np.sum((pos[parent_idx[parent_idx != -1]] - pos[parent_idx != -1]) ** 2, axis=1))
                edge_keys = ['euclidean_dist', 'path_length']

                # exclude pid =-1
                node_idx = (pid != -1)

                # create a list of edges of the form [(e1,e2, {'euclidean_dist': ec, 'path_length': pl}), ..]
                edge_data = list(zip(pid[node_idx], n[node_idx],
                                     [dict(zip(edge_keys, [ec[ix], ec[ix]])) for ix in range(ec.shape[0])]))

            G.add_nodes_from(node_data)
            G.add_edges_from(edge_data)

        self._G = G

        if G.nodes():

            self._remove_redundant_nodes()
            self._make_tree()   # needed to access the functions predecessor and successor

    def _remove_redundant_nodes(self):
        """
        Remove redundant nodes from the NeuronTree. A node is considered redundant if the edge between two nodes has a
        euclidean distance = 0, so the node has the same 3D position as its predecessor.
        """
        # get the nodes whose edge between them has distance = 0.
        nodeindices = np.array(list(self.get_edge_attributes('euclidean_dist').values())) == 0
        edgelist = list(np.array(list(self.get_edge_attributes('euclidean_dist').keys()))[nodeindices])
        while edgelist:
            predecessor, redundantNode = edgelist.pop()

            if self._nxversion == 2:
                # changed for version 2.x of networkX
                n1 = self._G.nodes[predecessor]
            else:
                n1 = self._G.node[predecessor]

            successors = self._G.successors(redundantNode)

            # connect edges across redundant nodes
            for succ in successors:
                if self._nxversion == 2:
                    # changed for version 2.x of networkX
                    n2 = self._G.nodes[succ]
                else:
                    n2 = self._G.node[succ]

                d = np.sqrt(np.sum((n1['pos'] - n2['pos']) ** 2))
                self._G.add_edge(predecessor, succ, euclidean_dist=d, path_length=d)

            # remove redundant node from graph
            self._G.remove_node(redundantNode)
            nodeindices = np.array(list(self.get_edge_attributes('euclidean_dist').values())) == 0
            edgelist = list(np.array(list(self.get_edge_attributes('euclidean_dist').keys()))[nodeindices])

    def _get_branch_type(self, B):
        """
        get the type of a branch based on majority vote.
        :param B: subgraph, a branch within the NeuronTree
        :return: int, type id (1: soma, 2: axon, 3: basal dendrite, 4: apical dendrite). Type of the branch 'B'.
        """
        Y = self.get_node_attributes('type')
        bt = []
        bt += [Y[k] for k in B]
        return np.round(np.mean(bt))

    def _unify_type(self):
        """
        Fixing routine: Set all branches originating from the soma to their majority vote type.
        """
        for s in self._G.successors(self.get_root()):
            S = nx.dfs_tree(self._G, s).nodes()
            t = self._get_branch_type(S)
            for k in S:
                if self._nxversion == 2:
                    # changed for version 2.x of networkX
                    self._G.nodes[k]['type'] = t
                else:
                    self._G.node[k]['type'] = t

    def _clean_axon(self):
        """
        Fixing routine: Only keep one axon originating from the soma. If there is multiple present only the longest
        neurite is kept ( in terms of number of edges) the other ones get deleted. The manipulation happens inplace.
        """
        # clean up axon: only longest axon is kept
        axon_edges = self.get_axon_edges()
        root = self.get_root()
        if axon_edges:
            axon_edges = np.array(axon_edges)
            edges = axon_edges[(axon_edges[:, 0] == root)]
            l = []

            for n in edges:
                l.append(len(nx.dfs_tree(self._G, n[root]).nodes()))

            m = max(l)
            ax = edges[l.index(m)][1]
            to_remove = edges[(edges[:, 1] != ax)]

            for e in to_remove:
                self._G.remove_nodes_from(nx.dfs_tree(self._G, e[1]).nodes())

    def make_pos_relative(self):
        """
        Deprecated! Makes all node positions relative to the soma. The soma will then have position (0,0,0).
        """
        root = self.get_root()
        if 'pos' in self.get_node_attribute_names():
            root_pos = self._G.node[root]['pos']

            for v in self._G.node:
                v_pos = self._G.node[v]['pos']
                self._G.node[v]['pos'] = v_pos - root_pos
        else:
            raise Warning('There is no position data assigned to the nodes.')

    def _make_tree(self):
        """
        Forces the networkx.DiGraph to be a tree. This routine was needed because the networkx.dfs_tree() function
        looses the original node and edge attributes. Manipulation done inplace.
        changed for use with networkx v2 (works also in old version: parameters with names)
        """

        G = self._G
        roots = self.get_root(return_all=True) # in case there are disconnected neurites

        trees = []
        for r in roots:
            trees.append(nx.dfs_tree(G, r))

        if len(trees) > 1:
            edges = []
            for t in trees:
                edges += t.edges()
            T = nx.from_edgelist(edges, create_using=nx.DiGraph())
        else:
            T = trees[0]

        for n_attr in self.get_node_attribute_names():
            attr = self.get_node_attributes(n_attr)
            nx.set_node_attributes(T, name=n_attr, values=attr)

        for e_attr in self.get_edge_attribute_names():
            attr = self.get_edge_attributes(e_attr)
            nx.set_edge_attributes(T, name=e_attr, values=attr)

        self._G = T

    def get_graph(self):
        return self._G

    def get_topological_minor(self):
        """
        Returns the topological minor of the Neuron. In this representation all continuation points are pruned away and
        the neuron solely consists of tips and branch points.
        Changed for use with networkx v2 (works also in old version: edge -> adj)
        :return:
            NeuronTree: mst. The topological minor representation of the original neuron.
        """
        # get the included nodes, which are soma, branch points and tips
        root = self.get_root()
        other_points = np.unique(np.append(self.get_branchpoints(), root))
        tips = self.get_tips()

        if self._nxversion == 2:
            # changed for version 2.x of networkX
            node_data = self.get_graph().nodes
        else:
            node_data = self.get_graph().node
        node_data_new = [(node, node_data[node]) for node in np.append(other_points, tips)]

        # get parent of each node and create edge_data
        nodes = set(tips)
        edge_data = self.get_graph().adj
        edge_data_new = []
        while nodes:
            current_node = nodes.pop()
            # if node is  not soma
            if current_node != root:
                cn = copy.copy(current_node)
                pred = list(nx.DiGraph.predecessors(self.get_graph(), current_node))[0]
                path_length = edge_data[pred][cn]['path_length']
                while pred not in other_points:
                    cn = pred
                    pred = list(nx.DiGraph.predecessors(self.get_graph(), pred))[0]
                    path_length += edge_data[pred][cn]['path_length']

                ec = np.sqrt(np.sum((node_data[current_node]['pos'] - node_data[pred]['pos']) ** 2))
                # add edge to edge_data_new
                edge_data_new.append((pred, current_node, dict(euclidean_dist=ec, path_length=path_length)))

                nodes.add(pred)  # adds the predecessor only once since nodes is a set
        return NeuronTree(node_data=node_data_new, edge_data=edge_data_new, nxversion=self._nxversion)

    def smooth_neurites(self, dim=1, window_size=21):

        from scipy.signal import savgol_filter

        G = copy.copy(self.get_graph())

        positions = self.get_node_attributes('pos')

        smoothed = dict(zip(G.nodes(), [False] * len(G.nodes())))
        r = self.get_root()
        smoothed[r] = True

        dist_ = nx.single_source_dijkstra_path_length(G, source=r, weight='path_length')
        tips = self.get_tips()

        # get tips sorted by path length in descending order
        to_visit = tips[np.argsort([dist_[t] for t in tips])[::-1]]

        # smoothing
        for c in to_visit:
            predecessors = [c]
            for p in predecessors:
                # add all unsmoothed predecessors
                add = [p_ for p_ in G.predecessors(p) if not smoothed[p_]]
                predecessors += add

            path = [positions[p] for p in predecessors]
            if len(path) > window_size:
                data = np.array(path)
                datahat = savgol_filter(data[:, dim], window_size, 3, mode='nearest').reshape(-1,1)

                if dim == 0:
                    datahat = np.hstack([datahat.reshape(-1, 1), data[:, [1,2]]])
                elif dim == 1:
                    datahat = np.hstack([data[:, 0].reshape(-1, 1),
                                         datahat.reshape(-1, 1),
                                         data[:, 2].reshape(-1, 1)])
                elif dim == 2:
                    datahat = np.hstack([data[:, [0,1]],datahat.reshape(-1, 1)])
                    
                positions.update(dict(zip(predecessors, datahat)))

            smoothed.update(dict(zip(predecessors, [True] * len(predecessors))))

        # Create a new tree
        e_attr = []
        for e in G.edges():
            d = np.sqrt(np.sum((positions[e[0]] - positions[e[1]]) ** 2))
            e_attr.append(d)

        nx.set_node_attributes(G, 'pos', positions)
        nx.set_edge_attributes(G, 'euclidean_dist', dict(zip(G.edges(), e_attr)))
        nx.set_edge_attributes(G, 'path_length', dict(zip(G.edges(), e_attr)))

        S = NeuronTree(node_data=G.nodes(data=True), edge_data=G.edges(data=True), nxversion=self._nxversion)
        return S

    def rename_nodes(self, label=None):
        """
        Renames the nodes within the graph. Note, this operation is done in place. If no label dictionary is passed the
        nodes are relabeled in consecutive order.
        :param label: optional. Dictionary holding the renaming {old_node_label: new_node_label}
        """

        nodes = list(self.nodes())
        nodes.sort()

        if label is None:
            label = dict(zip(nodes, range(1, len(nodes) + 1)))
        relabeled_G = nx.relabel.relabel_nodes(self.get_graph(), label)

        self._G = relabeled_G

    def get_node_attribute_names(self):
        """ returns the list of attributes assigned to each node.
            If no attributes are assigned it returns an empty list.
            changed for use with networkx v2 (works in all versions)
            :return:
                list : attribute names assigned to nodes
        """
        attr = []
        if self._G.nodes():
            if self._nxversion == 2:
                # changed for version 2.2 of networkX
                node_id = list(self._G.nodes().keys())[0]
                attr = list(self._G.nodes[node_id].keys())
            else:
                # this works only with version 1 of networkX
                node_id = self._G.nodes()[0]
                attr = list(self._G.node[node_id].keys())
        return attr

    def get_node_attributes(self, attribute):
        """
        Returns a dictionary that holds the attribute's value for each node.
        :param attribute: string. Possible values are "type", "pos" and "radius".
        :return: dict of the form {n : attribute_value}eturns a dictionary that holds the attributes value for each edge.
        :param attribute: string. Possible values are "euclidean_dist" and "path_length"
        :return: dict of the form {(u,v) : attribute_value}
        """
        return nx.get_node_attributes(self.get_graph(), attribute)

    def get_edge_attribute_names(self):
        """
            Returns the list of attributes assigned to each edge.
            If no attributes are assigned it returns an empty list.
            changed for use with networkx v2 (works in all versions)
        :return:
            list : attribute names assigned to edges
        """
        attr = []
        if self._G.edges():
            if self._nxversion == 2:
                # changed for version 2.2 of networkX
                e = list(self._G.edges().keys())[0]
            else:
                # this works only with version 1 of networkX
                e = self._G.edges()[0]
            attr = list(self._G.adj[e[0]][e[1]].keys())
        return attr

    def get_edge_attributes(self, attribute):
        """
        Returns a dictionary that holds the attribute's value for each edge.
        :param attribute: string. Possible values are "euclidean_dist" and "path_length"
        :return: dict of the form {(u,v) : attribute_value}
        """
        return nx.get_edge_attributes(self.get_graph(), attribute)

    def get_path_length(self, weight='euclidean_dist'):
        """
        Returns a dictionary containing the path length to the root for each node.
        :param: weight String (default='euclidean_dist'). Determines which edge attribute ist used. Options are
        'path_length' and 'eulidean_dist'. Note that in an unreduced tree, both options return the same values.
        :return:
         dict: Dictionary of the form {node_id=path length to soma}
        """
        pl = nx.single_source_dijkstra_path_length(self.get_graph(), source=self.get_root(), weight=weight)
        return pl

    def get_cumulative_path_length(self):
        """
        Returns a dictionary that hold the cumulative path length of the subtree attached to node n. The root holds
        the total path length within the tree whereas the cumulative path length of all tips is equal to zero.
        :return:
        dict: Dictionary holding the cumulative path length of the subtree connected to each node.
        """

        tips = self.get_tips()
        G = self.get_graph()
        nodes = self.nodes()
        c_pl = dict(zip(nodes, [0] * len(nodes)))

        active_nodes = list(tips)
        while len(active_nodes) > 0:
            a = active_nodes.pop(0)

            edges = G.edges(a, data=True)
            # for add pl of each edge coming from a
            for e1, e2, data in edges:
                c_pl[e1] += (c_pl[e2] + data['path_length'])

            # insert the parents
            parents = G.predecessors(a)
            active_nodes += parents
        return c_pl

    def get_root(self, return_all=False):
        """
        Returns the root of the Neuron's rooted tree graph. A root is defined as a node that has an in degree of 0.
        :param return_all: bool (defaut=False). Determines if all roots are returned or just one. If there is only one
        root it is returned as an int.
        :return: int or list , node id(s) of the tree roots.
        """
        if self._nxversion == 2:
            # changed for version 2.2 of networkX
            roots = [n for n, d in self._G.in_degree() if d == 0]
        else:
            roots = [n for n, d in self._G.in_degree().items() if d == 0]
        if return_all:
            return roots
        else:
            return roots[0]

    def is_connected(self):
        """
        This function returns True if the neuron is connected and False otherwise. In case of False it means that there
        are disconnected neurites in the reconstruction.

        For access to the disconnected components checkout networkx.connected_components(graph).
        :return: bool. True if all neurites are connected, False otherwise.
        """
        G = self.get_graph()
        connected = nx.connected.is_connected(G.to_undirected())
        return connected
        
    def has_root(self):
        """
        This function returns True if the neuron has a valid root node and False if not
        
        :return: bool. True if a root node is found, False otherwise.
        """
        roots = self.get_root(return_all=True)
        return len(roots) > 0

    def truncate_nodes(self, perc=.1, no_trunc_nodes=None):
        """
        Truncates the number of nodes by the given percentage. The nodes are pruned equally from the tips inward until
        the approximated percentage has been deleted or at least the given number of nodes is truncated.
        :param perc: float. Percent of nodes that are to be truncated between 0 and 1. Default=0.1
        :param no_trunc_nodes: int . Optional. Number of nodes to be truncated
        :return: The truncated tree.
        """
        T = copy.copy(self)
        nodes = self.nodes()
        total_no_nodes = len(nodes)

        if not no_trunc_nodes:
            no_trunc_nodes = int(total_no_nodes * perc)

        tips = T.get_tips()

        for t in tips:
            nodes.remove(t)

        while total_no_nodes - no_trunc_nodes < len(nodes):
            T = NeuronTree(graph=T.get_graph().subgraph(nodes), nxversion=self._nxversion)
            nodes = T.nodes()

            tips = T.get_tips()

            for t in tips:
                nodes.remove(t)
        
        return T

    def nodes(self, type_ix=None, data=False):
        """
        Wrapper function to the networkx.nodes() function. It returns a list of all nodes.
        :param type_ix: int, optional, default = None. Determines the type of nodes to be returned.
        Options are None (= all nodes), 2 (= axonal nodes) and 3 (= dendritic nodes).
        :param data: boolean, default=False. If set to True the node attribute data is returned as well.
        :return: list of nodes
        """

        if type_ix is None:
            nodes = list(self._G.nodes(data=data))
        else:
            if type(type_ix) == list:
                if self._nxversion == 2:
                    # changed for version 2.x of networkX
                    nodes = [k for k in self._G if self._G.nodes[k]['type'] in type_ix]
                else:
                    nodes = [k for k in self._G.nodes() if self._G.node[k]['type'] in type_ix]
            else:
                if self._nxversion == 2:
                    # changed for version 2.x of networkX
                    nodes = [k for k in self._G if self._G.nodes[k]['type'] == type_ix]
                else:
                    nodes = [k for k in self._G.nodes() if self._G.node[k]['type'] == type_ix]

        return nodes

    def edges(self, start=None, type_ix=None, data=False):
        """
        Wrapper function to the networkx.edges() function. It returns a list of edges.
        :param start: int, node id, determines the starting edge within the neuron
        :param type_ix: int, optional, default = None. Determines the type of edges to be returned.
        Options are None (= all edges), 2 (= axonal edges), 3 (= basal dendritic edges), 4 (= apical dendritic edges) or
        [3,4] (= all dendritic edges).
        :param data: boolean, default=False. If set to True the edge attribute data is returned as well.
        :return: list of edges
        """
        if type_ix is None:
            edges = list(self._G.edges(start, data=data))
        else:
            nodes = self.nodes(type_ix=type_ix)
            edges = [x for x in self._G.edges(start, data=data) if (x[0] in nodes or x[1] in nodes)]
        return edges

    def get_dendrite_nodes(self, data=False, type_ix=[3, 4]):
        """
        Returns all dendritic nodes.
        :param data: boolean, default=False. If set to True the node attribute data is returned as well.
        :param type_ix: list, default = [3,4]. This determines if all, only basal (ix =3) or only apical (ix = 4)
        dendritic nodes are returned.
        :return: list of dendritic nodes
        """
        return np.array(self.nodes(type_ix=type_ix , data=data))

    def get_axon_nodes(self, data=False):
        """
        Returns all axonal nodes.
        :param data: boolean, default=False. If set to True the node attribute data is returned as well.
        :return: list of axonal nodes
        """
        return np.array(self.nodes(type_ix=2, data=data))

    def get_axon_edges(self, start=None, data=False):
        axon_nodes = self.get_axon_nodes()
        return [x for x in self._G.edges(start,data=data) if (x[0] in axon_nodes or x[1] in axon_nodes)]

    def get_dendrite_edges(self, start=None, data=False):
        dendrite_nodes = self.get_dendrite_nodes()
        return [x for x in self._G.edges(start, data=data) if (x[0] in dendrite_nodes or x[1] in dendrite_nodes)]

    def get_tips(self):
        """
        Returns a list of tips, so the ending nodes within the neuron.
        changed for use with networkx v2 (works also in old version: edge -> adj)
        :return: list of node ids of tips.
        """
        E = self._G.adj
        return np.array([e for e in E if E[e] == {}])

    def get_branchpoints(self):
        """
        Returns a list of branch point ids of the neuron.
        :return: list of node ids of all branch points (nodes that have more than one successor).
        """
        # is this correct???:
        bp_indx = np.where(np.array(np.sum(nx.adjacency_matrix(self._G), axis=1)).flatten() > 1)[0]

        return np.array(self.nodes())[bp_indx]

    def get_dendritic_tree(self, type_ix=[3,4]):
        """
        Returns the dendrites as a new NeuronTree.
        :return: NeuronTree
        """
        nodes = list(self.get_dendrite_nodes(type_ix=type_ix))
        nodes.insert(0, self.get_root())
        subgraph = nx.subgraph(self._G, nodes)
        return NeuronTree(graph=subgraph, nxversion=self._nxversion)

    def get_axonal_tree(self):
        """
        Returns the axon as a new NeuronTree
        :return: NeuronTree
        """
        nodes = list(self.get_axon_nodes())
        nodes.insert(0, self.get_root())
        subgraph = nx.subgraph(self._G, nodes)
        return NeuronTree(graph=subgraph, nxversion=self._nxversion)

    def get_adjacency_matrix(self, weight=None):
        """
        Returns the adjacency matrix of the Tree saved in self._G. weight can be None, 'euclidean_dist' or 'path_length'
        :param weight: edge attribute that is considered for the adjacency matrix A. Default = None, then A only
        contains the structural connectivity between nodes. If set to 'euclidean_dist' or 'path_length' the adjacency
        matrix is weighted accordingly.
        :return: sparse array
        """
        return nx.adjancency_matrix(self._G, weight=weight)

    def get_extend(self, robust=False):
        """
        Returns the maximal extend in x, y and z direction.
        :param robust: bool. This parameter determines if the extend is calculated as maximum (default robust=False) or
        as 95-percentile (robust=True). The latter is considered a more robust value, as it reflects the extend of 95%
        of the mass.
        :return: 1x3 numpy.array
        """
        P = np.array(list(self.get_node_attributes('pos').values()))
        if robust:
            extend = np.percentile(P, 97.5, axis=0) - np.percentile(P, 2.5, axis=0)
        else:
            extend = np.max(P, axis=0) - np.min(P, axis=0)
        return extend

    def get_root_angles(self, angle_type='axis'):
        """
        Returns a dictionary of the root angle of each edge. Root angle denotes the orientation of each edge with
        respect to the root (soma).
        :param angle_type: either 'axis' or 'euler' (default='axis')
         Defines the type of angle that is calculated. Euler angles are defined as angles around the canonical
        euler axes (x, y and z). Axis angles are defined with respect to the rotation axis between two vectors.
        :return: dict of the form {(u,v): root_angle of edge between u and v}
        """
        angles = {}
        if angle_type == 'axis':
            func = lambda u, v: angle_between(u, v)
        elif angle_type == 'euler':
            func = lambda u, v: rotationMatrixToEulerAngles(get_rotation_matrix(u, v))
        else:
            raise NotImplementedError('Angle type %s is not implemented' % angle_type)

        for n1, n2 in self._G.edges():
            if self._nxversion == 1:
                u = self._G.node[n2]['pos'] - self._G.node[n1]['pos']
                v = self._G.node[n1]['pos'] - self._G.node[self.get_root()]['pos']
            else:
                u = self._G.nodes[n2]['pos'] - self._G.nodes[n1]['pos']
                v = self._G.nodes[n1]['pos'] - self._G.nodes[self.get_root()]['pos']

            angles.update({(n1,n2): func(u, v) * 180 / np.pi})
        return angles

    def get_branch_order(self):
        """
        Returns the dictionary of the branch order of each node. The branch order denotes the number of branch points
        that are crossed when tracing the path back to the soma.
        :return:
            d: dict
            Dictionary of the form {u: branch_order} for each node.
        """
        root = self.get_root()
        return self._get_branch_order(root, 0)

    def _get_branch_order(self, start, bo):
        """
        Returns the dictionary assigning the right branch order to each node.
        :param start: starting node
        :param bo: starting branch order
        :return:
            d: dict
            Dictionary of the form {u: branch_order} for each node reachable from starting node.
        """
        d = {}
        edges = list(self.edges(start))
        d[start] = bo
        if len(edges) > 1:
            for e in edges:
                d.update(self._get_branch_order(e[1], bo + 1))
        elif len(edges) == 1:
            d.update(self._get_branch_order(edges[0][1], bo))
        return d

    def get_strahler_order(self):
        """
        Returns the dictionary assigning the right Strahler order to each node.
        If the node is a leaf (has no children), its Strahler number is one.
        If the node has one child with Strahler number i, and all other children have Strahler numbers less than i,
        then the Strahler number of the node is i again.
        If the node has two or more children with Strahler number i, and no children with greater number,
        then the Strahler number of the node is i + 1. ( taken from https://en.wikipedia.org/wiki/Strahler_number)
        :return:
            dict. Dictionary of the form {u: Strahler order} for each node.
        """

        tips = self.get_tips()
        post_order = list(nx.dfs_postorder_nodes(self.get_graph(), self.get_root()))

        active = [n for n in post_order if n not in tips]
        strahler_order = dict(zip(tips, [1] * len(tips)))

        for a in active:
            so, counts = np.unique([strahler_order[s] for s in self._G.successors(a)], return_counts=True)
            # the highest child straher order only occurs once
            if counts[-1] == 1:
                strahler_order[a] = so[-1]
            else:
                strahler_order[a] = so[-1] + 1

        return strahler_order

    def _get_distance(self, dist='path_length', as_dict=True):
        """
        Returns the distance. Helper function for the distributions.
        :param dist: String, defines the distance measure to be used (default is 'path_length'), Options are
        'path_length', 'radial' and 'branch_order'.
        :param as_dict: boolean, default = True. Determines whether the distance are returned as a dictionary of the
        form {'node_id': ,distance} or as an numpy.array.
        :return: Dictionary or numpy.array of the defined distance measure from each node to the soma.
        """
        if dist == 'path_length':

            if as_dict:
                dist_ = self.get_path_length(weight='path_length')
            else:
                dist_ = np.array(list(self.get_path_length().values()))
        elif dist == 'branch_order':
            dist_ = self.get_branch_order()
            if not as_dict:
                dist_ = np.array(list(dist_.values()))

        elif dist == 'radial':
            dist_ = self.get_radial_distance()
            if not as_dict:
                dist_ = np.array(list(dist_.values()))
        else:
            raise NotImplementedError

        return dist_

    def get_radial_distance(self):
        """
        Returns a dictionary with radial distance from soma.
        :return: dict (node: radial distance to soma}
        """
        radial_dist = {}

        root = self.get_root()
        positions = self.get_node_attributes("pos")
        r = positions[root]

        radial_distance = lambda u: np.sqrt(np.dot(u - r, u - r))

        for n in self.nodes():
            n_pos = positions[n]
            radial_dist[n] = radial_distance(n_pos)
        return radial_dist

    def get_segment_length(self, dist='path_length'):
        """
        Returns the dictionary of the length in microns of each segment where dist denotes the distance measure. A segment
        is defined as the path between two branch points or a branch point and a tip.
         Possible options are 'path_length' an 'euclidean_dist'. The keys of the dictionary denote the
        tuples of the starting and end node of each segment.
        :param dist: String, options ['path_length', 'euclidean_dist']
        :return:
            d: dict
            Dictionary of the form {(n_s, n_e): segment length[u] in either 'path length' or 'euclidean distance' }
        """

        T = self.get_topological_minor()

        segment_length = T.get_edge_attributes(dist)
        return segment_length

    def get_histogram(self, value='branch_order', dist_measure=None, **kwargs):
        """

        :param value:
        :param dist_measure:
        :param kwargs:
        :return:
        """

        r = None
        dim = 1
        if value == 'branch_order':
            values = self.get_branch_order()

        elif value == 'strahler_order':
            values = self.get_strahler_order()

        elif value == 'branch_angle':
            values = self.get_branch_angles()

        elif value == 'path_angle':
            values = self.get_path_angles()

        elif value == 'root_angle':
            if 'angle_type' in kwargs.keys():
                angle_type = kwargs.pop('angle_type')
                if angle_type == 'euler':
                    dim = 3
                    r = [[0, 180]] * dim

                values = self.get_root_angles(angle_type)

            else:
                values = self.get_root_angles()


        elif value == 'thickness':
            values = self.get_radii()

        elif value == 'segment_length':
            values = self.get_segment_length()

        elif value == 'path_length':
            values = self.get_path_length()

        elif value == 'radial_dist':
            values = self.get_radial_distance()

        else:
            raise ValueError("There is value %s defined." % value)

        if dist_measure:
            distances = self._get_distance(dist_measure, as_dict=True)
            # if the respective values are given in a dictionary that is indexed by nodes
            if value in ['branch_order', 'strahler_order', 'branch_angle',
                         'path_angle', 'thickness', 'path_length', 'radial_dist']:
                dist = [distances[n] for n in values.keys()]
            else:  # otherwise
                dist = [distances[k[1]] for k in values.keys()]

            data = np.array(list(zip(dist, list(values.values()))))
            hist_data = np.histogramdd(data, range=r, **kwargs)

        else:
            # just query the values of each dictionary
            data = np.array(list(values.values()))

            if dim == 1:
                hist_data = np.histogram(data, range=r, **kwargs)
            else:
                hist_data = np.histogramdd(data, range=r, **kwargs)

        return hist_data

    def get_kde_distribution(self, key, dist=None):
        """
        Returns a Gaussian kernel density estimate of the distribution of the measure defined by _key_.
        changed for use with networkx v2 (works also in old version: edge -> adj)
        :param key: String, measure to calculate the kernel density estimate from. Options are: 'thickness',
        'path_angle', 'branch_angle', 'branch_order', 'strahler_order' and 'distance'.
        :param dist: Optional. Allows to express the measure as a function of distance. If set to None, the kde is
        one-dimensional, otherwise it is has two dimensions. Options for available distance measures,
        see _get_distance().
        :return: A one- or two-dimensional Gaussian kernel density estimate of measure _key_.
        """

        if key == 'thickness':
            thickness = np.array(list(self.get_node_attributes('radius').values()))

            if dist:
                data = np.array(list(zip(self._get_distance(dist), thickness)))
                return stats.gaussian_kde(data)

            else:
                return stats.gaussian_kde(thickness)
        elif key == 'path_angle':

            successors = nx.dfs_successors(self.get_graph(), 1)
            path_angle = []
            nodes = []
            for n1, n2 in self.edges():
                u = self.get_graph().node[n2]['pos'] - self.get_graph().node[n1]['pos']
                try:
                    for succ in successors[n2]:
                        v = self.get_graph().node[succ]['pos'] - self.get_graph().node[n2]['pos']
                        path_angle.append(angle_between(u, v) * 180 / np.pi)  # convert angles into degree
                        nodes.append(n2)
                except KeyError:
                    continue

            if dist:
                distance = self._get_distance(dist, as_dict=True)
                distance = [distance[n] for n in nodes]

                data = np.array(list(zip(distance, path_angle)))
                return stats.gaussian_kde(data)
            else:
                return stats.gaussian_kde(path_angle)

        elif key == 'branch_angle':
            branch_angles = []
            branchpoints = self.get_branchpoints()
            for bp in branchpoints:
                successors = list(self.get_graph().adj[bp].keys())
                branches = []
                for succ in successors:  # create a vector for each branching edge
                    v = self.get_graph().node[succ]['pos'] - self.get_graph().node[bp]['pos']
                    branches.append(v)
                for u, v in combinations(branches, 2):
                    branch_angles.append(angle_between(u, v) * 180 / np.pi)

            if dist:
                distance = self._get_distance(dist, as_dict=True)
                distance = [distance[bp] for bp in branchpoints]
                data = np.array(list(zip(distance, branch_angles)))
                return stats.gaussian_kde(data)
            else:
                return stats.gaussian_kde(branch_angles)

        elif key == 'branch_order':
            branch_order = list(self._get_branch_order(1, 0).values())

            if dist:
                data = np.array(list(zip(self._get_distance(dist), branch_order)))
                return stats.gaussian_kde(data)
            else:
                return stats.gaussian_kde(branch_order)

        elif key == 'strahler_order':
            strahler_order = list(self.get_strahler_order().values())

            if dist:
                data = np.array(list(zip(self._get_distance(dist), strahler_order)))
                return stats.gaussian_kde(data)
            else:
                return stats.gaussian_kde(strahler_order)

        elif key == 'distance':
            distance = np.array(list(nx.single_source_dijkstra_path_length(self.get_graph(), source=self.get_root(),
                                                                           weight='euclidean_dist').values()))

            if dist:
                data = np.array(list(zip(self._get_distance(dist), distance)))
                return stats.gaussian_kde(data)
            else:
                return stats.gaussian_kde(distance)
        else:
            raise NotImplementedError

    def get_radii(self):
        """
        Returns the radii of each node.
        :return: dict {u: thickness of u}
        """
        # get the thickness of each node
        thickness_dict = self.get_node_attributes('radius')
        return thickness_dict

    def get_path_angles(self):
        """
        Returns a dictionary of angles between two edges if their connecting point is no branch point.
        Angles are reported in degree.
        :return: dict of path angles btw two edges.
                d[v] holds the angle between edge (u,v) and (v,w)
        """
        # get the depth first search successors from the soma (id=1).
        root = self.get_root()
        branchpoints = list(self.get_branchpoints())
        if root in branchpoints:
            branchpoints.remove(root)
        successors = nx.dfs_successors(self.get_graph(), root)
        path_angle = {}

        for u, v in self.edges():

            if u not in branchpoints:
                if self._nxversion == 2:
                    # changed for version 2.x of networkX
                    e1 = self.get_graph().nodes[v]['pos'] - self.get_graph().nodes[u]['pos']
                else:
                    e1 = self.get_graph().node[v]['pos'] - self.get_graph().node[u]['pos']

                try:

                    for w in successors[v]:
                        if self._nxversion == 2:
                            # changed for version 2.x of networkX
                            e2 = self.get_graph().nodes[w]['pos'] - self.get_graph().nodes[v]['pos']
                        else:
                            e2 = self.get_graph().node[w]['pos'] - self.get_graph().node[v]['pos']

                        path_angle[v] = angle_between(e1, e2) * 180 / np.pi
                except KeyError:
                    continue

        return path_angle

    def get_soma_angles(self):
        """
        Returns the list of angles between the neurites exiting the soma.
        :return: soma_angles    list of angles (in degree).
        """

        from itertools import combinations

        r = self.get_root()
        successors = list(self.get_graph().adj[r].keys())

        branches = []
        for succ in successors:
            if self._nxversion == 2:
                # changed for version 2.x of networkX
                v = self.get_graph().nodes[succ]['pos'] - self.get_graph().nodes[r]['pos']
            else:
                v = self.get_graph().node[succ]['pos'] - self.get_graph().node[r]['pos']
            branches.append(v)
        soma_angles = []
        for u, v in combinations(branches, 2):
            # compute the angles between all possible combinations. Only keep the len(branches) - 1 smallest of them
            soma_angles.append(angle_between(u, v) * 180 / np.pi)
        soma_angles.sort()
        return soma_angles[:len(branches) - 1]

    def get_branch_angles(self):
        """
        Returns a dictionary of branch angles.
        changed for use with networkx v2 (works also in old version: edge -> adj)
        :return:
            branch_angles   dictionary of branch angles. Its form is {(bp, succ1, succ2): angle between (bp, succ1) and
            (bp, succ2).
        """

        from itertools import combinations
        from utils import angle_between

        branch_angles = {}
        branchpoints = list(self.get_branchpoints())

        # remove soma from the list of branch points
        soma = self.get_root()
        if soma in branchpoints:
            branchpoints.remove(soma)

        for bp in branchpoints:
            successors = list(self.get_graph().adj[bp].keys())
            branches = []
            for succ in successors:  # create a vector for each branching edge
                if self._nxversion == 2:
                    # changed for version 2.x of networkX
                    v = self.get_graph().nodes[succ]['pos'] - self.get_graph().nodes[bp]['pos']
                else:
                    v = self.get_graph().node[succ]['pos'] - self.get_graph().node[bp]['pos']
                branches.append((succ, v))
            all_angles = {}
            for u, v in combinations(branches, 2):
                # compute the angles between all possible combinations. Only keep the len(branches) - 1 smallest of them
                all_angles.update({(bp, u[0], v[0]): angle_between(u[1], v[1]) * 180 / np.pi})

            # sort by angles, only keep the lowest X values with x= no. branches -1
            sorted_angles = sorted(all_angles.items(), key=lambda item: item[1])
            branch_angles.update(dict(sorted_angles[:len(branches) - 1]))

        return branch_angles

    def get_volume(self):
        """
        Returns the volume for each segment in the tree.
        :return: dictionary of the form d[(u,v)] = volume(u,v)
        """

        d = {}
        for e in self.edges(data=True):

            h = e[2]['euclidean_dist']
            if self._nxversion == 2:
                # changed for version 2.x of networkX
                r = self.get_graph().nodes[e[0]]['radius']
                R = self.get_graph().nodes[e[1]]['radius']
            else:
                r = self.get_graph().node[e[0]]['radius']
                R = self.get_graph().node[e[1]]['radius']

            d[(e[0], e[1])] = (1/3)*np.pi*h*(r*r + r*R + R*R)
        return d

    def get_surface(self):
        """
        Returns the surface for each segment in the tree treating each edge as a pipe (without closing lids!).
        :return: dictionary of the form d[(u,v)] = surface(u,v)
        """
        d = {}
        for e in self.edges(data=True):
            h = e[2]['euclidean_dist']
            if self._nxversion == 2:
                # changed for version 2.x of networkX
                r = self.get_graph().nodes[e[0]]['radius']
                R = self.get_graph().nodes[e[1]]['radius']
            else:
                r = self.get_graph().node[e[0]]['radius']
                R = self.get_graph().node[e[1]]['radius']

            d[(e[0], e[1])] = np.pi*(r + R)*np.sqrt((R-r)**2 + h**2)
        return d

    def get_sholl_intersection_profile(self, proj='xy', steps=36, centroid='centroid'):
        """
        Calculates the Sholl intersection profile of the neurons projection determined by the parameter _proj_. The
        Sholl intersection profile counts the intersection of the neurites with concentric circles with increasing
        radii. The origin of the concentric circles can be chosen to either be the centroid of the
        projected neuron's convex hull or to be the soma.
        :param proj: 2D projection of all neurites. Options are 'xy', 'xz' and 'yz'
        :param steps: number of concentric circles centered around _centroid_. Their radii are determined as the
        respective fraction of the distance from the centroid to the farthest point of the convex hull.
        :param centroid: Determines the origin of the concentric circles. Options are 'centroid' or 'soma'.
        :return:
            intersections  list, len(steps), count of intersections with each circle.
            intervals list, len(steps +1), radii of the concentric circles.
        """

        G = self.get_graph()
        coordinates = []

        if proj == 'xy':
            indx = [0, 1]
        elif proj == 'xz':
            indx = [0, 2]
        elif proj == 'yz':
            indx = [1, 2]
        else:
            raise ValueError("Projection %s not implemented" % proj)

        # get the coordinates of the points
        for e in G.edges():
            p1 = np.round(G.node[e[0]]['pos'], 2)
            p2 = np.round(G.node[e[1]]['pos'], 2)
            coordinates.append((p1[indx], p2[indx]))

        # remove illegal points
        coords = [c for c in coordinates if (c[0][0] != c[1][0] or c[0][1] != c[1][1])]

        lines = MultiLineString(coords).buffer(0.0001)
        bounds = np.array(lines.bounds).reshape(2, 2).T
        if centroid == 'centroid':
            center = np.array(lines.convex_hull.centroid.coords[0])
            p_circle = Point(center)
        elif centroid == 'soma':
            center = G.node[self.get_root()]['pos'][indx]
            p_circle = Point(center)
        else:
            raise ValueError("Centroid %s is not defined" % centroid)

        # get the maximal absolute coordinates in x and y
        idx = np.argmax(np.abs(bounds), axis=1)
        r_max = np.linalg.norm(center - bounds[idx, [0, 1]])

        intersections = []
        intervals = [0]
        for k in range(1, steps + 1):

            r = (r_max / steps) * k
            c = p_circle.buffer(r).boundary

            i = c.intersection(lines)
            if type(i) in [Point, LineString]:
                intersections.append(1)
            else:
                intersections.append(len(i))
            intervals.append(r)

        return intersections, intervals

    def get_psad(self):
        """
        Returns the proportional sum of absolute deviations (PSAD) for each branching node in the neuron. The PSAD is
        a measure of topological tree asymmetry and is defined for a branching node p as
        PSAD_p = m/(2*(m-1)*(n-m) * sum_m |r_i - n/m|
        where m is the out_degree of node p, n is the number of leaves of the subtree starting at p and r_i is the
        number of leaves of the i_th subtree of p. For a more detailed definition see:
        Verwer, Ronald WH, and Jaap van Pelt.
        "Descriptive and comparative analysis of geometrical properties of neuronal tree structures."
        Journal of neuroscience methods 18.1-2 (1986): 179-206.

        :return:
            w:      dict boolean, dict of weights [0,1] indicating which subtrees have more than 3 leaves.
            psad:   dict, dictionary of the form {bp: PSAD} for each  in neuron.

        """

        def get_no_of_leaves(G, start, out_degree):
            """
            Helper-function: Returns the number of leaves that are attached to start point in graph G.
            downstream.
            :param G: nx.Digraph
            :param start: int   point to get the number of terminals from.
            :param out_degree:  dict . Dictionary of out degrees for each node in G.
            :return: int
            """
            t = 0
            if out_degree[start] == 0:
                t = 1
            else:
                for s in G.successors(start):
                    t += get_no_of_leaves(G, s, out_degree)
            return t

        G = self.get_graph()

        branchpoints = self.get_branchpoints()
        nodes = self.nodes()
        if self._nxversion == 1:
            out_degree = nx.DiGraph.out_degree(G)
        else:
            out_degree = dict(G.out_degree())

        degree = dict(zip(nodes, [0] * len(nodes)))
        for n in nodes:
            degree[n] = get_no_of_leaves(G, n, out_degree)

        psad = dict()
        for bp in branchpoints:
            m = out_degree[bp]
            n = degree[bp]

            PSAD = 0
            if m < n:  # only calculate for asymmetric trees
                for s in G.successors(bp):
                    PSAD += np.abs(degree[s] - n / m)
                PSAD = PSAD * m / (2 * (m - 1) * (n - m))
            psad[bp] = PSAD

        w = dict(zip(psad.keys(), [degree[n] > 3 for n in psad.keys()]))
        return w, psad

    def _resample_tree_data(self, dist=1):
        """
        Re-sample new nodes along the tree in equidistant distance dist (given in microns) and calculates the respective
        node and edge attributes to generate a new NeuronTree.
        All original branching points are kept!
        :param dist: distance (in microns) at which each neurite is resampled.
        :return:
            POS     list of positions
            PID     list of parent ids
            TYPE    list of types for each resampled node
            RADIUS  list of radii for each resampled node
            DIST
        """
        P = []
        PID = []
        TYPE = []
        RADIUS = []
        DIST = []

        pid = -1

        for (u, v, edata) in self._G.edges(data=True):
            n1 = self._G.node[u]
            n2 = self._G.node[v]
            e = edata['euclidean_dist']
            v = n2['pos'] - n1['pos']
            v /= np.linalg.norm(v)

            # line between two nodes
            g = lambda x: x * v + n1['pos']

            # radius function

            if n1['radius'] == n2['radius']:
                r = lambda x: n2['radius']
            else:
                x = [0, e]
                y = [n1['radius'], n2['radius']]
                r = interp1d(x, y)

            n = list(n1['pos'])

            if n in P:
                pid = P.index(n)
            else:
                P.append(n)
                PID.append(pid)
                pid += 1
                TYPE.append(n1['type'])
                DIST.append(0)
                RADIUS.append(n1['radius'])

            for a in range(1, int(e / dist)):
                P.append(list(g(a * dist)))
                PID.append(pid)
                pid = len(P) - 1
                TYPE.append(n2['type'])
                DIST.append(dist)
                RADIUS.append(np.array([r(a * dist)])[0])

            P.append(list(n2['pos']))
            PID.append(pid)
            pid += 1
            TYPE.append(n2['type'])
            DIST.append(e % dist)
            RADIUS.append(n2['radius'])

        P = np.array(P)

        return P, PID, TYPE, RADIUS, DIST

    def resample_tree(self, dist=1):
        """
         Re-sample new nodes along the tree in equidistant distance dist (given in microns) and return a new
         NeuronTree with the resampled data. Original branch points are kept.
        :param dist: distance (in microns) at which to resample
        :return: NeuronTree
        """

        (pos, pid, t, r, d) = self._resample_tree_data(dist)
        n_attr = [dict(pos=pos[i], type=t[i], radius=r[i]) for i in range(len(d))]

        nodes = list(zip(range(1, len(pid) + 1), n_attr))

        e_attr = [dict(euclidean_dist=d[i], path_length=d[i]) for i in range(len(d))]
        edges = list(zip(np.array(pid) + 1, range(1, len(pid) + 1), e_attr))

        T = NeuronTree(node_data=nodes, edge_data=edges[1:], nxversion=self._nxversion)

        return T

    def get_neurites(self, soma_included=True):
        """
        Returns a list of all neurites extending from the soma (axon and dendrites). If the neurites are disconnected
        only the part connected to the soma will be returned.
        :param soma_included: bool. Determines if the soma is part of the neurites or not.
        :return: list of NeuronTrees
        """

        roots = self.get_root(return_all=True)
        soma_nodes = self.nodes(type_ix=1)
        r = [x for x in roots if x in soma_nodes][0]  # get somatic root node

        neurite_paths = dict()
        G = self.get_graph()

        # get the path of each neurite extending from the soma
        for t in self.get_tips():
            try:
                path = nx.dijkstra_path(G, r, t)
                stem_ix = path[1]

                if stem_ix in neurite_paths.keys():
                    # if traversing the same neurite to another tip, just append the path
                    neurite_paths[stem_ix] = neurite_paths[stem_ix].union(set(path))
                else:
                    # if tracing a new neurite
                    neurite_paths[stem_ix] = set(path)
            except:
                continue

        # get subgraph with all nodes
        subgraphs = []
        for key in neurite_paths.keys():
            nodes = neurite_paths[key]
            if not soma_included:
                nodes.remove(r)
            s = nx.subgraph(G, nodes)
            subgraphs.append(NeuronTree(graph=s))

        return subgraphs

#######################################################################################################################
#######################################################################################################################
#######################################################################################################################
    ########################################## DRAWING FUNCTIONS #############################################
#######################################################################################################################
#######################################################################################################################
#######################################################################################################################

    def get_node_colors(self):
        """
        Returns a list of colors for each node as it appears in the graph. Used to give the 2D graph representation
        node colors.
        :return: list of color char tags for each node in graph. 'g' for axon, 'y' for dendrite and 'grey' for soma.
        """
        axon_nodes = self.get_axon_nodes()
        dendrite_nodes = self.get_dendrite_nodes()

        colors = []
        for node in self._G.nodes():
            if node in axon_nodes:
                colors.append('g')
            elif node in dendrite_nodes:
                colors.append('y')
            else:
                colors.append('grey')
        return colors

    def draw_3D(self, fig=None, ix=111, reverse=True, r_axis='z', axon_color='grey', dendrite_color='darkgrey'):
        """
        Draws a stick figure neuron in 3D.
        :param fig: figure in which to draw. If None a new figure is created.
        :param ix: index of the subplot within the figure 'fig'. Default: 111
        :param reverse: Default=True. Determines whether the axis specified in 'r_axis' is inverted.
        :param r_axis: Default = 'z'. Defines the axis that is inverted if 'reverse' is set to True. Possible values are
        'x', 'y' and 'z'.
        :param axon_color: Color, default='grey'. Defines the color of the axon.
        :param dendrite_color: Color, default='darkgrey'. Defines the color of the dendrites.
        """

        nodes = [k for k in self.get_node_attributes('pos').values()]
        nodes = np.array(nodes)

        t = [axon_color if k == 2 else dendrite_color for k in self.get_node_attributes('type').values()]

        # plot G
        if not fig:
            fig = plt.figure()
        ax = fig.add_subplot(ix, projection='3d')
        ax.scatter(nodes[:, 0], nodes[:, 1], nodes[:, 2], c=t, marker='.')
        plt.hold
        root_pos = self._G.node[self.get_root()]['pos']
        ax.scatter(root_pos[0], root_pos[1], root_pos[2], c='k', marker='^')

        colors = ['k', axon_color, dendrite_color]

        for k, e in enumerate(self._G.edges()):
            n1 = self._G.node[e[0]]
            n2 = self._G.node[e[1]]
            v = np.array([n1['pos'], n2['pos']])

            ax.plot3D(v[:, 0], v[:, 1], v[:, 2], c=colors[int(n2['type']) - 1])

        ax = plt.gca()
        if reverse:
            if 'z' in r_axis:
                ax.set_zlim(ax.get_zlim()[::-1])
            if 'x' in r_axis:
                ax.set_xlim(ax.get_xlim()[::-1])
            if 'y' in r_axis:
                ax.set_ylim(ax.get_ylim()[::-1])

        ax.set_xlabel('X [microns]')
        ax.set_ylabel('Y [microns]')
        ax.set_zlabel('Z [microns]')

    def draw_2D(self, fig=None, ax=None, projection='xz', axon_color='darkgreen', dendrite_color='darkgrey',
                apical_dendrite_color='grey', x_offset=0, y_offset=0, **kwargs):
        """
        Plots a 2D projection of the stick figure neuron.
        :param fig: Figure in which to draw. If None a new figure is created.
        :param projection: Default='xz'. Identifier of the ewo dimensional projection plane ['xz', 'xy', 'yz']
        :param axon_color: Color, default='grey'. Defines the color of the axon.
        :param dendrite_color: Color, default='darkgrey'. Defines the color of the dendrites.
        :param x_offset: double, default = 0, define offset for the first projection coordinate.
        :param y_offset: double, default = 0, define offset for the first projection coordinate.
        :param kwargs: further plotting parameters that can be passed to the plt.plot function.
        """

        if not ax:
            if not fig:
                fig = plt.figure()
                ax = fig.gca()
            else:
                ax = fig.gca()

        if projection == 'xy':
            indices = [0,1]
        elif projection == 'xz':
            indices = [0,2]
        elif projection == 'yz':
            indices = [1,2]
        elif projection == 'yx':
            indices=[1,0]
        elif projection == 'zx':
            indices = [2,0]
        elif projection == 'zy':
            indices = [2,1]
        else:
            raise ValueError('projection %s is not defined.'% projection)

        G = self.get_graph()
        V = np.zeros((len(G.edges()), 2, 3))
        colors = ['k', axon_color, dendrite_color, apical_dendrite_color]

        cs = []
        
        if self._nxversion ==1:
            node_dict = G.node
        else:
            node_dict= G.nodes()
        
        for k, e in enumerate(G.edges()):
            n1 = node_dict[e[0]]
            n2 = node_dict[e[1]]
            V[k, 0] = n1['pos']
            V[k, 1] = n2['pos']
            cs.append(colors[int(n2['type']) - 1])

        cs = np.array(cs)
        x = indices[0]
        y = indices[1]

        plt_idx = np.array([cs_i == axon_color for cs_i in cs])
        if len(plt_idx.shape) > 1:
            plt_idx = plt_idx[:, 0]

        if plt_idx.any():
            _ = ax.plot(V[plt_idx, :, x].T + x_offset, V[plt_idx, :, y].T + y_offset, c=axon_color, **kwargs)

        plt_idx = np.array([cs_i == dendrite_color for cs_i in cs])
        if len(plt_idx.shape) > 1:
            plt_idx = plt_idx[:, 0]

        if plt_idx.any():
            _ = ax.plot(V[plt_idx, :, x].T + x_offset, V[plt_idx, :, y].T + y_offset, c=dendrite_color, **kwargs)

        plt_idx = np.array([cs_i == apical_dendrite_color for cs_i in cs])
        if len(plt_idx.shape) > 1:
            plt_idx = plt_idx[:, 0]

        if plt_idx.any():
            _ = ax.plot(V[plt_idx, :, x].T + x_offset, V[plt_idx, :, y].T + y_offset, c=apical_dendrite_color,
                               **kwargs)


    ############# SAVING FUNCTIONS #####################


    def to_swc(self):
        """
        Write NeuronTree into swc file compatible pandas.DataFrame.
        :return: pandas.DataFrame with columns 'n', 'type', 'x', 'y', 'z', 'radius' and 'parent'
        """

        # create dataframe with graph data
        G = self._G
        ids = [int(k) for k in G.nodes()]
        ids.sort()
        pos_dict = self.get_node_attributes('pos')
        r_dict = self.get_node_attributes('radius')
        t_dict = self.get_node_attributes('type')

        # create a parent dictionary
        beg = np.array([e[0] for e in self.edges()])
        end = np.array([e[1] for e in self.edges()])

        parents = {e: b for b, e in zip(beg, end)}
        parents[self.get_root()] = -1

        pids = [parents[e] for e in ids]
        pos = np.array([np.round(pos_dict[k], 2) for k in ids])
        r = np.array([r_dict[k] for k in ids])
        t = np.array([int(t_dict[k]) for k in ids])

        # write graph into swc file
        d = {'n': ids, 'type': t, 'x': pos[:, 0], 'y': pos[:, 1], 'z': pos[:, 2], 'radius': r, 'parent': pids}
        df = pd.DataFrame(data=d, columns=['n', 'type', 'x','y', 'z' ,'radius' , 'parent'])

        return df

    def write_to_swc(self, file_name,
                     path='/gpfs01/berens/data/data/anatomy/BC_morphologies/swc_tree/'):
        """
        Write NeuronTree to swc file.
        :param file_name: String. File name without '.swc' tag.
        :param path: String. Path to file

        """

        if not exists(path):
            makedirs(path)

        df = self.to_swc()
        df.to_csv(path + file_name + '.swc', sep=' ', encoding='utf-8', header=False, index=False)

    def write_to_mat(self, file_name,
                     path='/gpfs01/berens/data/data/anatomy/BC_morphologies/csv_luxburg/'):
        """
        Write NeuronTree to mat file.
        :param file_name: String. Filename without .mat tag.
        :param path:  String. Path to file
        """

        if not exists(path):
            makedirs(path)

        data = {}
        P = list(self.get_node_attributes('pos').values())
        T = list(self.get_node_attributes('type').values())
        A = nx.adjacency_matrix(G, weight='path_length')

        data['pos'] = P
        data['type'] = T
        data['A'] = A
        savemat(path + file_name + '.mat', data)


