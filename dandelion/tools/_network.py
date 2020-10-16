#!/usr/bin/env python
# @Author: Kelvin
# @Date:   2020-08-12 18:08:04
# @Last Modified by:   Kelvin
# @Last Modified time: 2020-09-18 16:40:14

import pandas as pd
import numpy as np
import networkx as nx
from polyleven import levenshtein
from ..utilities._utilities import *
from networkx.utils import random_state
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import minimum_spanning_tree
from scipy.spatial.distance import pdist, squareform
from tqdm import tqdm
from time import sleep
try:
    from scanpy import logging as logg
except ImportError:
    pass

def generate_network(self, distance_mode='simple', min_size=2, aa_or_nt=None, clone_key = None, constructbygroup = False, clones_sep = None, weights = None, layout = None):
    """
    Generates a Levenshtein distance network based on gapped full length sequences for heavy and light chain(s).
    The distance matrices are then combined into a singular matrix where a minimum spanning tree will be constructed per clone group specified by separator in `clones_sep` option.

    Parameters
    ----------
    data : Dandelion, DataFrame, str
        `Dandelion` object, pandas `DataFrame` in changeo/airr format, or file path to changeo/airr file after clones have been determined.
    distance_mode : str
        The mode of calculating joint distance matrix for heavy and light chains. Default is 'simple'. If 'simple', a simple sum operation will be used. If 'weighted', depending on whether `weights` option is provided, it will scale each layer to range of 0 to 1 to bring the multiple layers of data into a single analysis.
    min_size : int
        For visualization purposes, two graphs are created where one contains all cells and a trimmed second graph. This value specifies the minimum number of edges required otherwise node will be trimmed in the secondary graph.
    aa_or_nt : str, optional
        Option accepts 'aa', 'nt' or None, with None defaulting to 'aa'. Determines whether amino acid or nucleotide sequences will be used for calculating distances.
    clone_key: str, optional
        column name to build network on.
    constructbygroup: bool
        whether to link up by clone_group id. Default is False.
    clones_sep: tuple[int, str]
        A tuple containing how the clone groups should be extracted. None defaults to (0, '_').
    weights : tuple, optional
        A tuple containing weights to scale each layer. default is None where each layer is scaled evenly i.e. 1/number of layers.
    layout_option : str, optional
        choice of layout algorithm. None defaults to fruchterman reingold layout.
    Returns
    ----------
        `Dandelion` object with `.distance`, `.edges`, `.layout`, `.graph` initialized.
    """
    start = logg.info('Generating network')
    if self.__class__ == Dandelion:
        dat = load_data(self.data)
    else:
        dat = load_data(self)
    if clone_key is None:
        clonekey = 'clone_id'
    else:
        clonekey = clone_key
    if clonekey not in dat.columns:
        raise TypeError('Data does not contain clone information. Please run find_clones.')

    # calculate distance
    dat_h = dat[dat['locus'] == 'IGH']
    dat_l = dat[dat['locus'].isin(['IGK', 'IGL'])]
    if aa_or_nt is None or aa_or_nt is 'aa':
        seq_h = dict(zip(dat_h['sequence_id'], zip(dat_h['cell_id'], dat_h['sequence_alignment_aa'])))
        seq_l = dict(zip(dat_l['sequence_id'], zip(dat_l['cell_id'], dat_l['sequence_alignment_aa'])))
    elif aa_or_nt == 'nt':
        seq_h = dict(zip(dat_h['sequence_id'], zip(dat_h['cell_id'], dat_h['sequence_alignment'])))
        seq_l = dict(zip(dat_l['sequence_id'], zip(dat_l['cell_id'], dat_l['sequence_alignment'])))
    else:
        raise ValueError("aa_or_nt only accepts string values 'aa', 'nt' or None, with None defaulting to 'aa'.")

    # So first, create a data frame to hold all possible (full) sequences split by heavy (only 1 possible) and light (multiple possible)
    dat_seq = pd.DataFrame.from_dict(seq_h, orient = 'index', columns = ['cell_id', 'heavy'])
    dat_seq.set_index('cell_id', inplace = True)
    light_seq_tree = Tree()
    for key, value in seq_l.items():
        k, v = value
        light_seq_tree[k][key] = v
    light_seq_tree2 = Tree()
    for g in light_seq_tree:
        second_key = []
        for k2 in light_seq_tree[g].keys():
            second_key.append(k2)
        second_key = list(set(second_key))
        second_key_dict = dict(zip(second_key, range(0,len(second_key))))
        for key, value in light_seq_tree[g].items():
            light_seq_tree2[g][second_key_dict[key]] = value
    dat_seq['light'] = pd.Series(light_seq_tree2)
    tmp = pd.Series([dict(i) if i is not np.nan else {0:i} for i in dat_seq['light']])
    tmp_dat = pd.DataFrame(tmp.tolist(), index = dat_seq.index)

    tmp_dat.columns = ['light_' + str(c) for c in tmp_dat.columns]
    dat_seq = dat_seq.merge(tmp_dat, left_index = True, right_index = True)
    dat_seq = dat_seq[['heavy'] + [str(c) for c in tmp_dat.columns]]

    # calculate a distance matrix for all vs all and this can be referenced later on to extract the distance between the right pairs
    dmat = Tree()
    for x in tqdm(dat_seq.columns, desc = 'Calculating distances... '):
        seq_list = []
        seq_list = [y for y in dat_seq[x]]
        tdarray = np.array(seq_list).reshape(-1,1)
        d_mat = squareform(pdist(tdarray,lambda x,y: levenshtein(x[0],y[0])))
        dmat[x] = d_mat
    dist_mat_list = [dmat[x] for x in dmat if type(dmat[x]) is np.ndarray]

    n_ = len(dist_mat_list)
    if distance_mode == 'simple':
        total_dist = np.sum(dist_mat_list,axis=0)
    if distance_mode == 'weighted':
        weighted_matrix = []
        if weights is None:
            for w in range(0, n_):
                weighted_matrix.append(1/n_ * dist_mat_list[w])
            total_dist = sum(weighted_matrix)
        else:
            if len(weights) == n_:
                for w in range(0, n_):
                    weighted_matrix.append(weights[w] * dist_mat_list[w])
                total_dist = sum(weighted_matrix)
            else:
                raise IndexError('Length of provided weights should be %s.' % int(n_))

    # generate edge list    
    if self.__class__ == Dandelion:
        out = self.copy()
    else: # re-initiate a Dandelion class object
        out = Dandelion(dat)

    tmp_totaldist = pd.DataFrame(total_dist, index = out.metadata.index, columns = out.metadata.index)
    tmp_clusterdist = Tree()
    for i in out.metadata.index:
        if constructbygroup:
            cx = out.metadata.loc[i, str(clonekey)+'_group']
        else:
            cx = out.metadata.loc[i, str(clonekey)]
        tmp_clusterdist[cx][i].value = 1
    tmp_clusterdist2 = {}
    for x in tmp_clusterdist:
        tmp_clusterdist2[x] = list(tmp_clusterdist[x])
    cluster_dist = {}
    for x in tmp_clusterdist2:
        dist_mat_ = tmp_totaldist.loc[tmp_clusterdist2[x], tmp_clusterdist2[x]]
        s1, s2 = dist_mat_.shape
        if s1 > 1 and s2 >1:
            cluster_dist[x] = dist_mat_
    # to improve the visulisation and plotting efficiency, i will build a minimum spanning tree for each group/clone to connect the shortest path
    mst_tree = mst(cluster_dist)
    sleep(0.5)

    edge_list = Tree()
    for c in tqdm(mst_tree, desc = 'Generating edge list '):
        G = nx.from_pandas_adjacency(mst_tree[c], create_using=nx.MultiDiGraph())
        G.edges(data=True)
        edge_list[c] = nx.to_pandas_edgelist(G)
    sleep(0.5)
    clone_ref = dict(out.metadata[clonekey])
    tmp_clone_tree = Tree()
    for x in out.metadata.index:
        tmp_clone_tree[clone_ref[x]][x].value = 1
    tmp_clone_tree2 = Tree()
    for x in tmp_clone_tree:
        tmp_clone_tree2[x] = list(tmp_clone_tree[x])

    tmp_clone_tree3 = Tree()
    for x in tmp_clone_tree2:
        tmp_ = pd.DataFrame(index = tmp_clone_tree2[x], columns = tmp_clone_tree2[x])
        tmp_ = pd.DataFrame(np.tril(tmp_) + 1, index = tmp_clone_tree2[x], columns = tmp_clone_tree2[x])
        tmp_.fillna(0, inplace = True)
        tmp_clone_tree3[x] = tmp_

    # here I'm using a temporary edge list to catch all cells that were identified as clones to forecfully link them up if they were clipped off during the mst step
    tmp_edge_list = Tree()
    for c in tqdm(tmp_clone_tree3, desc = 'Linking edges '):
        G = nx.from_pandas_adjacency(tmp_clone_tree3[c], create_using=nx.MultiDiGraph())
        G.edges(data=True)
        tmp_edge_list[c] = nx.to_pandas_edgelist(G)

    # try to catch situations where there's no edge (only singletons)
    try:
        edge_listx = pd.concat([edge_list[x] for x in edge_list])
        edge_listx.index = [(s, t) for s, t in zip(edge_listx['source'],edge_listx['target'])]

        tmp_edge_listx = pd.concat([tmp_edge_list[x] for x in tmp_edge_list])
        tmp_edge_listx.index = [(s, t) for s, t in zip(tmp_edge_listx['source'], tmp_edge_listx['target'])]

        edge_list_final = edge_listx.combine_first(tmp_edge_listx)

        for idx in edge_list_final.index:
            edge_list_final.at[idx, 'weight'] = tmp_totaldist.loc[idx[0], idx[1]]
        # return the edge list
        edge_list_final.reset_index(drop = True, inplace = True)
    except:
        edge_list_final = pd.DataFrame(columns = ['source', 'target','weight'])

    # and finally the vertex list which is super easy
    vertice_list = list(out.metadata.index)

    # and now to actually generate the network
    g, g_, lyt, lyt_ = generate_layout(vertice_list, edge_list_final, min_size = min_size, weight = None)

    # convert distance matrices to sparse
    for x in dmat:
        if type(dmat[x]) is np.ndarray:
            dmat[x] = csr_matrix(dmat[x])

    logg.info(' finished', time=start,
        deep=('Updated Dandelion object: \n'
        '   \'data\', contig-indexed clone table\n'
        '   \'metadata\', cell-indexed clone table\n'
        '   \'distance\', heavy and light chain distance matrices\n'
        '   \'edges\', network edges\n'
        '   \'layout\', network layout\n'
        '   \'graph\', network'))
    if self.__class__ == Dandelion:
        if self.germline is not None:
            germline_ = self.germline
        else:
            germline_ = None
        if self.threshold is not None:
            threshold_ = self.threshold
        else:
            threshold_ = None
        self.__init__(data = self.data, metadata = self.metadata, distance = dmat, edges = edge_list_final, layout = (lyt, lyt_), graph = (g, g_), germline = germline_, initialize = False)
        self.threshold = threshold_
    else:
        out = Dandelion(data = dat, distance = dmat, edges = edge_list_final, layout = (lyt, lyt_), graph = (g, g_), clone_key = clone_key)
        return(out)

def mst(mat):
    """
    Construct minimum spanning tree based on supplied matrix in dictionary.

    Parameters
    ----------
    mat : dict
        Dictionary containing numpy ndarrays.
    Returns
    ----------
        Dandelion `Tree` object holding DataFrames of constructed minimum spanning trees.
    """
    mst_tree = Tree()
    for c in mat:
        mst_tree[c] = pd.DataFrame(minimum_spanning_tree(np.triu(mat[c])).toarray().astype(int), index = mat[c].index, columns = mat[c].columns)
    return(mst_tree)

def clone_degree(self, weight='weight'):
    start = logg.info('Calculating clone degree')
    if self.__class__ == Dandelion:
        dist = np.sum([self.distance[x].toarray() for x in self.distance if type(self.distance[x]) is csr_matrix], axis = 0)
        A = csr_matrix(dist)
        G = nx.Graph()
        G.add_weighted_edges_from(zip(list(self.metadata.index), list(self.metadata.index), A.data))
        cd = pd.DataFrame.from_dict(G.degree(weight = weight))
        cd.set_index(0, inplace = True)
        self.metadata['clone_degree'] = pd.Series(cd[1])
    else:
        raise TypeError('Input object must be of {}'.format(Dandelion))
    logg.info(' finished', time=start,
        deep=('Updated Dandelion metadata\n'))

def generate_layout(vertices, edges, min_size = 2, weight = None):
    G = nx.Graph()
    G.add_nodes_from(vertices)
    G.add_weighted_edges_from([(x,y,z) for x,y,z in zip(edges['source'], edges['target'], edges['weight'])])
    degree = G.degree()
    G_ = G.copy()
    if min_size == 2:
        G_.remove_nodes_from(nx.isolates(G))
    elif min_size > 2:
        remove = [node for node, degree in dict(G.degree()).items() if degree > min_size]
        G_.remove_nodes_from(remove)
    edges_, weights_ = zip(*nx.get_edge_attributes(G_,'weight').items())
    print('generating network layout')
    pos = _fruchterman_reingold_layout(G, weight = weight)
    pos_ = _fruchterman_reingold_layout(G_, weight = weight)
    return(G, G_, pos, pos_)

# when dealing with a lot of unconnected vertices, the pieces fly out to infinity and the original fr layout can't be used
# work around from https://stackoverflow.com/questions/14283341/how-to-increase-node-spacing-for-networkx-spring-layout
# code chunk from networkx's layout.py https://github.com/networkx/networkx/blob/master/networkx/drawing/layout.py
def _process_params(G, center, dim):
    # Some boilerplate code.

    if not isinstance(G, nx.Graph):
        empty_graph = nx.Graph()
        empty_graph.add_nodes_from(G)
        G = empty_graph

    if center is None:
        center = np.zeros(dim)
    else:
        center = np.asarray(center)

    if len(center) != dim:
        msg = "length of center coordinates must match dimension of layout"
        raise ValueError(msg)

    return G, center

def _fruchterman_reingold_layout(
    G,
    k=None,
    pos=None,
    fixed=None,
    iterations=50,
    threshold=1e-4,
    weight="weight",
    scale=1,
    center=None,
    dim=2,
    seed=None,
):
    """Position nodes using Fruchterman-Reingold force-directed algorithm.
    The algorithm simulates a force-directed representation of the network
    treating edges as springs holding nodes close, while treating nodes
    as repelling objects, sometimes called an anti-gravity force.
    Simulation continues until the positions are close to an equilibrium.
    There are some hard-coded values: minimal distance between
    nodes (0.01) and "temperature" of 0.1 to ensure nodes don't fly away.
    During the simulation, `k` helps determine the distance between nodes,
    though `scale` and `center` determine the size and place after
    rescaling occurs at the end of the simulation.
    Fixing some nodes doesn't allow them to move in the simulation.
    It also turns off the rescaling feature at the simulation's end.
    In addition, setting `scale` to `None` turns off rescaling.
    Parameters
    ----------
    G : NetworkX graph or list of nodes
        A position will be assigned to every node in G.
    k : float (default=None)
        Optimal distance between nodes.  If None the distance is set to
        1/sqrt(n) where n is the number of nodes.  Increase this value
        to move nodes farther apart.
    pos : dict or None  optional (default=None)
        Initial positions for nodes as a dictionary with node as keys
        and values as a coordinate list or tuple.  If None, then use
        random initial positions.
    fixed : list or None  optional (default=None)
        Nodes to keep fixed at initial position.
        ValueError raised if `fixed` specified and `pos` not.
    iterations : int  optional (default=50)
        Maximum number of iterations taken
    threshold: float optional (default = 1e-4)
        Threshold for relative error in node position changes.
        The iteration stops if the error is below this threshold.
    weight : string or None   optional (default='weight')
        The edge attribute that holds the numerical value used for
        the edge weight.  If None, then all edge weights are 1.
    scale : number or None (default: 1)
        Scale factor for positions. Not used unless `fixed is None`.
        If scale is None, no rescaling is performed.
    center : array-like or None
        Coordinate pair around which to center the layout.
        Not used unless `fixed is None`.
    dim : int
        Dimension of layout.
    seed : int, RandomState instance or None  optional (default=None)
        Set the random state for deterministic node layouts.
        If int, `seed` is the seed used by the random number generator,
        if numpy.random.RandomState instance, `seed` is the random
        number generator,
        if None, the random number generator is the RandomState instance used
        by numpy.random.
    Returns
    -------
    pos : dict
        A dictionary of positions keyed by node
    Examples
    --------
    >>> G = nx.path_graph(4)
    >>> pos = nx.spring_layout(G)
    # The same using longer but equivalent function name
    >>> pos = nx.fruchterman_reingold_layout(G)
    """

    G, center = _process_params(G, center, dim)

    if fixed is not None:
        if pos is None:
            raise ValueError("nodes are fixed without positions given")
        for node in fixed:
            if node not in pos:
                raise ValueError("nodes are fixed without positions given")
        nfixed = {node: i for i, node in enumerate(G)}
        fixed = np.asarray([nfixed[node] for node in fixed])

    if pos is not None:
        # Determine size of existing domain to adjust initial positions
        dom_size = max(coord for pos_tup in pos.values() for coord in pos_tup)
        if dom_size == 0:
            dom_size = 1
        pos_arr = seed.rand(len(G), dim) * dom_size + center

        for i, n in enumerate(G):
            if n in pos:
                pos_arr[i] = np.asarray(pos[n])
    else:
        pos_arr = None
        dom_size = 1

    if len(G) == 0:
        return {}
    if len(G) == 1:
        return {nx.utils.arbitrary_element(G.nodes()): center}

    try:
        # Sparse matrix
        if len(G) < 500:  # sparse solver for large graphs
            raise ValueError
        A = nx.to_scipy_sparse_matrix(G, weight=weight, dtype="f")
        if k is None and fixed is not None:
            # We must adjust k by domain size for layouts not near 1x1
            nnodes, _ = A.shape
            k = dom_size / np.sqrt(nnodes)
        pos = _sparse_fruchterman_reingold(
            A, k, pos_arr, fixed, iterations, threshold, dim, seed
        )
    except ValueError:
        A = nx.to_numpy_array(G, weight=weight)
        if k is None and fixed is not None:
            # We must adjust k by domain size for layouts not near 1x1
            nnodes, _ = A.shape
            k = dom_size / np.sqrt(nnodes)
        pos = _fruchterman_reingold(
            A, k, pos_arr, fixed, iterations, threshold, dim, seed
        )
    if fixed is None and scale is not None:
        pos = _rescale_layout(pos, scale=scale) + center
    pos = dict(zip(G, pos))
    return pos

@random_state(7)
def _fruchterman_reingold(
    A, k=None, pos=None, fixed=None, iterations=50, threshold=1e-4, dim=2, seed=None
):
    # Position nodes in adjacency matrix A using Fruchterman-Reingold
    # Entry point for NetworkX graph is fruchterman_reingold_layout()
    import numpy as np

    try:
        nnodes, _ = A.shape
    except AttributeError as e:
        msg = "fruchterman_reingold() takes an adjacency matrix as input"
        raise nx.NetworkXError(msg) from e

    if pos is None:
        # random initial positions
        pos = np.asarray(seed.rand(nnodes, dim), dtype=A.dtype)
    else:
        # make sure positions are of same type as matrix
        pos = pos.astype(A.dtype)

    # optimal distance between nodes
    if k is None:
        k = np.sqrt(1.0 / nnodes)
    # the initial "temperature"  is about .1 of domain area (=1x1)
    # this is the largest step allowed in the dynamics.
    # We need to calculate this in case our fixed positions force our domain
    # to be much bigger than 1x1
    t = max(max(pos.T[0]) - min(pos.T[0]), max(pos.T[1]) - min(pos.T[1])) * 0.1
    # simple cooling scheme.
    # linearly step down by dt on each iteration so last iteration is size dt.
    dt = t / float(iterations + 1)
    delta = np.zeros((pos.shape[0], pos.shape[0], pos.shape[1]), dtype=A.dtype)
    # the inscrutable (but fast) version
    # this is still O(V^2)
    # could use multilevel methods to speed this up significantly
    for iteration in range(iterations):
        # matrix of difference between points
        delta = pos[:, np.newaxis, :] - pos[np.newaxis, :, :]
        # distance between points
        distance = np.linalg.norm(delta, axis=-1)
        # enforce minimum distance of 0.01
        np.clip(distance, 0.001, None, out=distance)
        # displacement "force"
        displacement = np.einsum("ijk,ij->ik", delta, (k * k / distance ** 2 - A * distance / k))
        displacement = displacement - pos / ( k * np.sqrt(nnodes))
        # update positions
        length = np.linalg.norm(displacement, axis=-1)
        length = np.where(length < 0.01, 0.1, length)
        delta_pos = np.einsum("ij,i->ij", displacement, t / length)
        if fixed is not None:
            # don't change positions of fixed nodes
            delta_pos[fixed] = 0.0
        pos += delta_pos
        # cool temperature
        t -= dt
        err = np.linalg.norm(delta_pos) / nnodes
        if err < threshold:
            break
    return pos

@random_state(7)
def _sparse_fruchterman_reingold(
    A, k=None, pos=None, fixed=None, iterations=50, threshold=1e-4, dim=2, seed=None
):
    # Position nodes in adjacency matrix A using Fruchterman-Reingold
    # Entry point for NetworkX graph is fruchterman_reingold_layout()
    # Sparse version
    import numpy as np

    try:
        nnodes, _ = A.shape
    except AttributeError as e:
        msg = "fruchterman_reingold() takes an adjacency matrix as input"
        raise nx.NetworkXError(msg) from e
    try:
        from scipy.sparse import coo_matrix
    except ImportError as e:
        msg = "_sparse_fruchterman_reingold() scipy numpy: http://scipy.org/ "
        raise ImportError(msg) from e
    # make sure we have a LIst of Lists representation
    try:
        A = A.tolil()
    except AttributeError:
        A = (coo_matrix(A)).tolil()

    if pos is None:
        # random initial positions
        pos = np.asarray(seed.rand(nnodes, dim), dtype=A.dtype)
    else:
        # make sure positions are of same type as matrix
        pos = pos.astype(A.dtype)

    # no fixed nodes
    if fixed is None:
        fixed = []

    # optimal distance between nodes
    if k is None:
        k = np.sqrt(1.0 / nnodes)
    # the initial "temperature"  is about .1 of domain area (=1x1)
    # this is the largest step allowed in the dynamics.
    t = max(max(pos.T[0]) - min(pos.T[0]), max(pos.T[1]) - min(pos.T[1])) * 0.1
    # simple cooling scheme.
    # linearly step down by dt on each iteration so last iteration is size dt.
    dt = t / float(iterations + 1)

    displacement = np.zeros((dim, nnodes))
    for iteration in range(iterations):
        displacement *= 0
        # loop over rows
        for i in range(A.shape[0]):
            if i in fixed:
                continue
            # difference between this row's node position and all others
            delta = (pos[i] - pos).T
            # distance between points
            distance = np.sqrt((delta ** 2).sum(axis=0))
            # enforce minimum distance of 0.01
            distance = np.where(distance < 0.01, 0.01, distance)
            # the adjacency matrix row
            Ai = np.asarray(A.getrowview(i).toarray())
            # displacement "force"
            displacement[:, i] += (
                delta * (k * k / distance ** 2 - Ai * distance / k)
            ).sum(axis=1)
        displacement = displacement - pos / ( k * np.sqrt(nnodes))
        # update positions
        length = np.sqrt((displacement ** 2).sum(axis=0))
        length = np.where(length < 0.01, 0.1, length)
        delta_pos = (displacement * t / length).T
        pos += delta_pos
        # cool temperature
        t -= dt
        err = np.linalg.norm(delta_pos) / nnodes
        if err < threshold:
            break
    return pos

def _rescale_layout(pos, scale=1):
    """Returns scaled position array to (-scale, scale) in all axes.
    The function acts on NumPy arrays which hold position information.
    Each position is one row of the array. The dimension of the space
    equals the number of columns. Each coordinate in one column.
    To rescale, the mean (center) is subtracted from each axis separately.
    Then all values are scaled so that the largest magnitude value
    from all axes equals `scale` (thus, the aspect ratio is preserved).
    The resulting NumPy Array is returned (order of rows unchanged).
    Parameters
    ----------
    pos : numpy array
        positions to be scaled. Each row is a position.
    scale : number (default: 1)
        The size of the resulting extent in all directions.
    Returns
    -------
    pos : numpy array
        scaled positions. Each row is a position.
    """
    # Find max length over all dimensions
    lim = 0  # max coordinate for all axes
    for i in range(pos.shape[1]):
        pos[:, i] -= pos[:, i].mean()
        lim = max(abs(pos[:, i]).max(), lim)
    # rescale to (-scale, scale) in all directions, preserves aspect
    if lim > 0:
        for i in range(pos.shape[1]):
            pos[:, i] *= scale / lim
    return pos

def extract_edge_weights(self, full_graph = False):
    if full_graph:
        edges,weights = zip(*nx.get_edge_attributes(self.graph[0],'weight').items())
    else:
        edges,weights = zip(*nx.get_edge_attributes(self.graph[1],'weight').items())
    return(weights)