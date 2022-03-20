# a base class for UMAP to run on cpu or gpu
import numpy as np, pandas as pd
from time import time
from typing import Any, List, Optional, Union
from . import constants as config
from .ai_utils import setup_logger
from .feature_utils import prune_weighted_edges_df_and_relabel_nodes
logger = setup_logger(name=__name__, verbose=False)


###############################################################################


import_exn = None
try:
    import umap
    has_dependancy = True
except ModuleNotFoundError as e:
    logger.debug(
        f"UMAP not found, trying running `pip install graphistry[ai]`",
        exc_info=True
    )
    import_exn = e
    has_dependancy = False
    umap = Any

def assert_imported():
    if not has_dependancy:
        raise import_exn


###############################################################################


umap_kwargs_probs = {
    "n_components": 2,
    "metric": "hellinger",  # info metric, can't use on textual encodings since they contain negative values...
    "n_neighbors": 15,
    "min_dist": 0.3,
    "verbose": True,
    "spread": 0.5,
    "local_connectivity": 1,
    "repulsion_strength": 1,
    "negative_sample_rate": 5,
}

umap_kwargs_euclidean = {
    "n_components": 2,
    "metric": "euclidean",
    "n_neighbors": 12,
    "min_dist": 0.1,
    "verbose": True,
    "spread": 0.5,
    "local_connectivity": 1,
    "repulsion_strength": 1,
    "negative_sample_rate": 5,
}


def umap_graph_to_weighted_edges(umap_graph, cfg=config):
    logger.debug("Calculating weighted adjacency (edge) DataFrame")
    coo = umap_graph.tocoo()
    src, dst, weight_col = cfg.SRC, cfg.DST, cfg.WEIGHT

    _weighted_edges_df = pd.DataFrame(
        {src: coo.row, dst: coo.col, weight_col: coo.data}
    )

    return _weighted_edges_df


class UMAPMixin(object):
    """
        UMAP Mixin for automagic UMAPing
        
    """

    def __init__(self):
        self.umap_initialized = False
        pass

    def umap_lazy_init(
        self,
        n_neighbors: int = 12,
        min_dist: float = 0.1,
        spread=0.5,
        local_connectivity=1,
        repulsion_strength=1,
        negative_sample_rate=5,
        n_components: int = 2,
        metric: str = "euclidean",
    ):

        #FIXME remove as set_new_kwargs will always replace?

        if has_dependancy and not self.umap_initialized:

            umap_kwargs = dict(
                n_components=n_components,
                metric=metric,
                n_neighbors=n_neighbors,
                min_dist=min_dist,
                spread=spread,
                local_connectivity=local_connectivity,
                repulsion_strength=repulsion_strength,
                negative_sample_rate=negative_sample_rate,
            )
    
            self.n_components = n_components
            self.metric = metric
            self.n_neighbors = n_neighbors
            self.min_dist = min_dist
            self.spread = spread
            self.local_connectivity = local_connectivity
            self.repulsion_strength = repulsion_strength
            self.negative_sample_rate = negative_sample_rate
            self._umap = umap.UMAP(**umap_kwargs)

            self.umap_initialized = True


    #TODO should this cascade with umap_lazy_init?
    def _set_new_kwargs(self, **kwargs):
        assert_imported()
        self._umap = umap.UMAP(**kwargs)

    def _check_target_is_one_dimensional(self, y: Union[np.ndarray, None]):
        if y is None:
            return None
        if y.ndim == 1:
            return y
        elif y.ndim == 2 and y.shape[1] == 1:
            return y
        else:
            logger.warning(
                f"* Ignoring target column of shape {y.shape} as it is not one dimensional"
            )
            return None

    #FIXME rename to umap_fit
    def fit(self, X: np.ndarray, y: Union[np.ndarray, None] = None):
        t = time()
        y = self._check_target_is_one_dimensional(y)
        logger.info(f"Starting UMAP-ing data of shape {X.shape}")
        self._umap.fit(X, y)
        self._weighted_edges_df = umap_graph_to_weighted_edges(self._umap.graph_)
        self._weighted_adjacency = self._umap.graph_
        mins = (time() - t) / 60
        logger.info(f"-UMAP-ing took {mins:.2f} minutes total")
        logger.info(f" - or {X.shape[0]/mins:.2f} rows per minute")
        return self

    #FIXME rename to umap_fit_transform
    def fit_transform(self, X: np.ndarray, y: Union[np.ndarray, None] = None):
        self.fit(X, y)
        return self._umap.transform(X)

    def umap(
        self,
        kind: str = "nodes",
        use_columns: Union[List, None] = None,
        featurize: bool = True,
        encode_position: bool = True,
        encode_weight: bool = True,
        inplace: bool = False,
        X: np.ndarray = None,
        y: Union[np.ndarray, List] = None,
        scale: float = 0.1,
        n_neighbors: int = 12,
        min_dist: float = 0.1,
        spread: float = 0.5,
        local_connectivity: int = 1,
        repulsion_strength: float = 1,
        negative_sample_rate: int = 5,
        n_components: int = 2,
        metric: str = "euclidean",
        scale_xy: float = 10,
        suffix: str = "",
        play: Optional[int] = 0,
        engine: str = "umap_learn",
    ):
        """
            UMAP the featurized node or edges data, or pass in your own X, y (optional).

        :param kind: `nodes` or `edges` or None. If None, expects explicit X, y (optional) matrices, and will Not
                associate them to nodes or edges. If X, y (optional) is given, with kind = [nodes, edges],
                it will associate new matrices to nodes or edges attributes.
        :param use_columns: List of columns to use for featurization if featurization hasn't been applied.
        :param featurize: Whether to re-featurize, or use previous features, and just slice into appropriate columns
        :param encode_weight: if True, will set new edges_df from implicit UMAP, default True.
        :param encode_position: whether to set default plotting bindings -- positions x,y from umap for .plot()
        :param X: ndarray of features
        :param y: ndarray of targets
        :param scale: multiplicative scale for pruning weighted edge DataFrame gotten from UMAP (mean + scale *std)
        :param n_neighbors: UMAP number of nearest neighbors to include for UMAP connectivity, lower makes more compact layouts. Minimum 2.
        :param min_dist: UMAP float between 0 and 1, lower makes more compact layouts.
        :param spread: UMAP spread of values for relaxation
        :param local_connectivity: UMAP connectivity parameter
        :param repulsion_strength: UMAP repulsion strength
        :param negative_sample_rate: UMAP negative sampling rate
        :param n_components: number of components in the UMAP projection, default 2
        :param metric: UMAP metric, default 'euclidean'. Other useful ones are 'hellinger', '..'
                see (UMAP-LEARN)[https://umap-learn.readthedocs.io/en/latest/parameters.html] documentation for more.
        :param suffix: optional suffix to add to x, y attributes of umap.
        :param play: Graphistry play parameter, default 0, how much to evolve the network during clustering
        :param engine: selects which engine to use to calculate UMAP: NotImplemented yet, default UMAP-LEARN
        :return: self, with attributes set with new data
        """
        assert_imported()
        self.umap_lazy_init()

        self.suffix = suffix
        xy = None
        umap_kwargs = dict(
            n_components=n_components,
            metric=metric,
            n_neighbors=n_neighbors,
            min_dist=min_dist,
            spread=spread,
            local_connectivity=local_connectivity,
            repulsion_strength=repulsion_strength,
            negative_sample_rate=negative_sample_rate,
        )

        if inplace:
            res = self
        else:
            res = self.bind()

        res._set_new_kwargs(**umap_kwargs)

        if kind == "nodes":

            #FIXME not sure if this is preserving the intent
            #... when should/shouldn't we relabel? 
            index_to_nodes_dict = None
            if res._node is None:
                res = res.nodes(
                    res._nodes
                        .reset_index(drop=True).reset_index()
                        .rename(columns={'index': config.IMPLICIT_NODE_ID}),
                    config.IMPLICIT_NODE_ID
                )
                nodes = res._nodes[res._node].values
                index_to_nodes_dict = dict(zip(range(len(nodes)), nodes))

            X, y = res._featurize_or_get_nodes_dataframe_if_X_is_None(
                X, y, use_columns, featurize=featurize
            )
            xy = scale_xy * res.fit_transform(X, y)
            res.weighted_adjacency_nodes = res._weighted_adjacency
            res.node_embedding = xy
            # TODO add edge filter so graph doesn't have double edges
            # TODO user-guidable edge merge policies like upsert?
            res.weighted_edges_df_from_nodes = (
                prune_weighted_edges_df_and_relabel_nodes(
                    res._weighted_edges_df,
                    scale=scale,
                    index_to_nodes_dict=index_to_nodes_dict,
                )
            )
        elif kind == "edges":
            X, y = res._featurize_or_get_edges_dataframe_if_X_is_None(
                X, y, use_columns, featurize=featurize
            )
            xy = scale_xy * res.fit_transform(X, y)
            res.weighted_adjacency_edges = res._weighted_adjacency
            res.edge_embedding = xy
            res.weighted_edges_df_from_edges = (
                prune_weighted_edges_df_and_relabel_nodes(
                    res._weighted_edges_df, scale=scale, index_to_nodes_dict=None
                )
            )
        elif kind is None:
            logger.warning(
                f"kind should be one of `nodes` or `edges` unless you are passing explicit matrices"
            )
            if X is not None:
                logger.info(f"New Matrix `X` passed in for UMAP-ing")
                xy = res.fit_transform(X, y)
                res._xy = xy
                res._weighted_edges_df = prune_weighted_edges_df_and_relabel_nodes(
                    res._weighted_edges_df, scale=scale
                )
                logger.info(
                    f"Reduced Coordinates are stored in `._xy` attribute and "
                    f"pruned weighted_edge_df in `._weighted_edges_df` attribute"
                )
            else:
                logger.error(
                    f"If `kind` is `None`, `X` and optionally `y` must be given"
                )
        else:
            raise ValueError(
                f"`kind` needs to be one of `nodes`, `edges`, `None`, got {kind}"
            )
        res = self._bind_xy_from_umap(res, kind, encode_position, encode_weight, play)
        if not inplace:
            return res

    def _bind_xy_from_umap(
        self,
        res: Any,
        kind: str,
        encode_position: bool,
        encode_weight: bool,
        play: Optional[int],
    ):
        # todo make sure xy is two dim, might be 3 or more....
        df = res._nodes if kind == "nodes" else res._edges

        df = df.copy(deep=False)
        x_name = config.X + self.suffix
        y_name = config.Y + self.suffix
        if kind == "nodes":
            emb = res.node_embedding
        else:
            emb = res.edge_embedding
        df[x_name] = emb.T[0]
        df[y_name] = emb.T[1]

        res = res.nodes(df) if kind == "nodes" else res.edges(df)

        if encode_weight and kind == "nodes":
            w_name = config.WEIGHT + self.suffix
            umap_df = res.weighted_edges_df_from_nodes.copy(deep=False)
            umap_df = umap_df.rename({config.WEIGHT: w_name})
            res = res.edges(umap_df, config.SRC, config.DST)
            logger.info(
                f"Wrote new edges_dataframe from UMAP embedding of shape {res._edges.shape}"
            )
            res = res.bind(edge_weight=w_name)

        if encode_position and kind == "nodes":
            if play is not None:
                return res.bind(point_x=x_name, point_y=y_name).layout_settings(
                    play=play
                )
            else:
                return res.bind(point_x=x_name, point_y=y_name)

        return res