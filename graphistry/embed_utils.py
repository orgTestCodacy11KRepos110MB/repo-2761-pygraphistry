import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import dgl
from dgl.dataloading import GraphDataLoader
import torch.nn.functional as F
from .networks import RGCNEmbed
from tqdm import trange
import logging
from typing import Optional, Union, Callable, List

tt = torch.Tensor
XSymbolic = Optional[Union[List[str], str, pd.DataFrame]]
ProtoSymbolic = Optional[Union[str, Callable[[tt, tt, tt], tt]]]

logging.StreamHandler.terminator = ""
logger = logging.getLogger(__name__)
#logger.setLevel(logging.DEBUG)


def log(msg:str) -> None:
    # setting every logs to WARNING level
    logger.log(msg=msg, level=30)


class EmbedDistScore:
    @staticmethod
    def TransE(h:tt, r:tt, t:tt) -> tt:
        return (h + r - t).norm(p=1, dim=1)

    @staticmethod
    def DistMult(h:tt, r:tt, t:tt) -> tt:
        return (h * r * t).sum(dim=1)

    @staticmethod
    def RotatE(h:tt, r:tt, t:tt) -> tt:
        return -(h * r - t).norm(p=1, dim=1)


class HeterographEmbedModuleMixin(nn.Module):
    def __init__(self):
        super().__init__()

        self.protocol = {
            "TransE": EmbedDistScore.TransE,
            "DistMult": EmbedDistScore.DistMult,
            "RotatE": EmbedDistScore.RotatE,
        }

    def _preprocess_embedding_data(self, train_split:Optional[Union[float, int]]=0.8) -> None:
        log('Preprocessing embedding data')
        src, dst = self._source, self._destination
        relation = self._relation

        if self._node is not None and self._nodes is not None:
            nodes = self._nodes[self._node]
        elif self._node is None and self._nodes is not None:
            nodes = self._nodes.reset_index(drop=True).reset_index()["index"]
        else:
            nodes = pd.Series(pd.concat([self._edges[src], self._edges[dst]]).unique())

        edges = self._edges
        edges = edges[edges[src].isin(nodes) & edges[dst].isin(nodes)]
        relations = edges[relation].unique()

        # type2id
        self._node2id = {n: idx for idx, n in enumerate(nodes)}
        self._relation2id = {r: idx for idx, r in enumerate(relations)}

        self._id2node = {idx: n for idx, n in enumerate(nodes)}
        self._id2relation = {idx: r for idx, r in enumerate(relations)}

        s, r, t = (
            edges[src].map(self._node2id),
            edges[relation].map(self._relation2id),
            edges[dst].map(self._node2id),
        )
        triplets = torch.from_numpy(pd.concat([s, r, t], axis=1).to_numpy())

        # split idx
        if not hasattr(self, "train_idx") or self._train_split != train_split:
            log(msg="--Splitting data")
            train_size = int(train_split * len(triplets))
            test_size = len(triplets) - train_size
            train_dataset, test_dataset = torch.utils.data.random_split(
                triplets, [train_size, test_size]
            )
            self.train_idx = train_dataset.indices
            self.test_idx = test_dataset.indices

        self.triplets = triplets
        self._num_nodes, self._num_rels = (len(self._node2id), len(self._relation2id))
        log(
            f"--num_nodes: {self._num_nodes}, \
                num_relationships: {self._num_rels}"
        )

    def _build_graph(self) -> None:
        s, r, t = self.triplets.T
        g_dgl = dgl.graph(
            (s[self.train_idx], t[self.train_idx]), num_nodes=self._num_nodes
        )
        g_dgl.edata[dgl.ETYPE] = r[self.train_idx]
        g_dgl.edata["norm"] = dgl.norm_by_dst(g_dgl).unsqueeze(-1)

        self.g_dgl = g_dgl

    def _init_model(self, batch_size:int, device:Union['str', torch.device]) -> Union[nn.Module, GraphDataLoader]:
        g_iter = SubgraphIterator(self.g_dgl)
        g_dataloader = GraphDataLoader(
            g_iter, batch_size=batch_size, collate_fn=lambda x: x[0]
        )

        # init model
        model = HeteroEmbed(
            self._num_nodes,
            self._num_rels,
            self._embed_dim,
            proto=self.proto,
            node_features=self._node_features,
            device=device,
        )

        return model, g_dataloader

    def _train_embedding(self, epochs:int, batch_size:int, lr:float, device:Union['str', torch.device]):
        log('Training embedding')
        model, g_dataloader = self._init_model(batch_size, device)
        if hasattr(self, "_embed_model"):
            model = self._embed_model
            log("--Reusing previous model")

        optimizer = torch.optim.Adam(model.parameters(), lr=lr)
        pbar = trange(epochs, desc=None)
        model.to(device)

        score = 0
        for epoch in pbar:
            model.train()
            for data in g_dataloader:
                g, edges, labels = data

                g = g.to(device)
                edges = edges.to(device)
                labels = labels.to(device)

                emb = model(g)
                loss = model.loss(emb, edges, labels)
                optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optimizer.step()
                pbar.set_description(
                    f"epoch: {epoch+1}, loss: {loss.item():.4f}, score: {score:.4f}%"
                )

            model.eval()
            self._embeddings = model(self.g_dgl.to(device)).detach()
            self._embed_model = model
            if self._eval_flag:
                score = self._eval(threshold=0.5)
                pbar.set_description(
                    f"epoch: {epoch+1}, loss: {loss.item():.4f}, score: {score:.2f}%"
                )

        return self

    def embed(
        self,
        relation:str,
        proto:ProtoSymbolic='DistMult',
        embedding_dim:Optional[int]=32,
        use_feat:Optional[bool]=False,
        X:XSymbolic=None,
        epochs:Optional[int]=2,
        batch_size:Optional[int]=32,
        train_split:Optional[Union[float, int]]=0.8,
        lr:Optional[float]=1e-2,
        inplace:Optional[bool]=False,
        device:Optional[Union[str, torch.device]]="cpu",
        evaluate:Optional[bool]=True,
        *args,
        **kwargs,
    ):
        """Embed a graph using a relational graph convolutional network (RGCN),
        and return a new graphistry graph with the embeddings as node
        attributes.


        Parameters
        ----------
        relation : str
            column to use as relation between nodes
        proto : ProtoSymbolic
            metric to use, ['TransE', 'RotateE', 'DistMult'] or provide your own. Defaults to 'DistMult'.
        embedding_dim : Optional[int]
            relation embedding dimension. defaults to 32
        use_feat : Optional[bool]
            wether to featurize nodes, if False will produce random embeddings and shape them during training.
            Defaults to True
        X : XSymbolic
            Which columns in the nodes dataframe to featurize. Inherets args from graphistry.featurize().
            Defaults to None.
        epochs : Optional[int]
            Number of training epochs. Defaults to 2
        batch_size : Optional[int]
            batch_size. Defaults to 32
        train_split : Optional[Union[float, int]]
            train percentage, between 0, 1. Defaults to 0.8.
        lr : Optional[float]
            learning rate. Defaults to 0.002
        inplace : Optional[bool]
            inplace
        device : Optional[Union[str, torch.device]]
            accelarator. Defaults to "cpu"
        evaluate : Optional[bool]
            Whether to evaluate. Defaults to False.

        Returns
        -------
            self : graphistry instance
        """
        if inplace:
            res = self
        else:
            res = self.bind()

        res._relation = relation
        res._use_feat = use_feat
        res._embed_dim = embedding_dim
        res._train_split = train_split
        res._eval_flag = evaluate

        if callable(proto):
            res.proto = proto
        else:
            res.proto = res.protocol[proto]

        if res._use_feat and res._nodes is not None:
            # todo decouple self from res
            res = res.featurize(kind="nodes", X=X, *args, **kwargs)

        if not hasattr(self, "triplets"):
            res._preprocess_embedding_data(train_split=train_split)
            res._build_graph()

        return res._train_embedding(epochs, batch_size, lr=lr, device=device)

    def calculate_prob(
        self, test_triplet, test_triplets, threshold, h_r, node_embeddings, infer=None
    ):
        # TODO: simplify
        if infer == "all":
            s, r, o_ = test_triplet
        else:
            s, r = test_triplet

        subject_relation = test_triplet[:2]
        num_entity = len(node_embeddings)
        delete_idx = torch.sum(h_r == subject_relation, dim=1)
        delete_idx = torch.nonzero(delete_idx == 2).squeeze()
        delete_entity_idx = test_triplets[delete_idx, 2].view(-1).numpy()
        perturb_entity_idx = np.array(
            list(set(np.arange(num_entity)) - set(delete_entity_idx))
        )
        perturb_entity_idx = torch.from_numpy(perturb_entity_idx).squeeze()

        if infer == "all":
            perturb_entity_idx = torch.cat((perturb_entity_idx, torch.unsqueeze(o_, 0)))

        o = self.proto(
            node_embeddings[s],
            self._embed_model.relational_embedding[r],
            node_embeddings[perturb_entity_idx],
        )

        score = torch.sigmoid(o)
        return perturb_entity_idx[score > threshold]

    def _predict(self, test_triplets, threshold=0.95, directed=True, infer=None):

        if type(test_triplets) != torch.Tensor:
            test_triplets = torch.tensor(test_triplets)
        triplets = self.triplets

        s, r, o = triplets.T
        edge_index = torch.stack([s, o])

        # make graph
        g = dgl.graph((s, o), num_nodes=edge_index.max() + 1)
        g.edata[dgl.ETYPE] = r
        g.edata["norm"] = dgl.norm_by_dst(g).unsqueeze(-1)
        del s, r, o

        node_embeddings = self._embed_model(g)

        h_r = triplets[:, :2]
        t_r = torch.stack((triplets[:, 2], triplets[:, 1])).transpose(0, 1)

        visited, predicted_links = {}, []
        for test_triplet in test_triplets:
            s, r, o_ = test_triplet
            k = "".join([str(s), "_", str(r)])
            kr = "".join([str(r), "_", str(s)])

            # for [s, r] -> {d}
            if k not in visited:

                links = self.calculate_prob(
                    test_triplet, test_triplets, threshold, h_r, node_embeddings, infer
                )
                visited[k] = ""
                predicted_links += [
                    [
                        self._id2node[s.item()],
                        self._id2relation[r.item()],
                        self._id2node[i.item()],
                    ]
                    for i in links
                ]

            # for [d, r] -> {s}
            if kr not in visited and not directed:
                links = self.calculate_prob(
                    test_triplet, test_triplets, threshold, t_r, node_embeddings, infer
                )
                visited[k] = ""
                predicted_links += [
                    [
                        self._id2node[s.item()],
                        self._id2relation[r.item()],
                        self._id2node[i.item()],
                    ]
                    for i in links
                ]

        predicted_links = pd.DataFrame(
            predicted_links, columns=[self._source, self._relation, self._destination]
        )
        return predicted_links, node_embeddings

    def predict_link(
        self,
        test_df: pd.DataFrame,
        src:str,
        rel:str,
        threshold:Optional[float] = 0.95
    ) -> pd.DataFrame:
        """predict links from a test dataframe given src/dst and rel columns

        Parameters
        ----------
        test_df : pd.DataFrame
            dataframe of test data
        src : str
            source column name
        rel : str
            relation column name
        threshold : Optional[float]
            Probability threshold/confidence. Defaults to 0.95.

        Returns
        -------
        pd.DataFrame
            dataframe containing predicted links

        """
        pred = "predicted_destination"
        nodes = test_df[src].map(self._node2id)
        relations = test_df[rel].map(self._relation2id)

        all_nodes = self._node2id.values()
        test_df = pd.concat([nodes, relations], axis=1)
        test_df[pred] = [all_nodes] * len(test_df)
        test_df = test_df.explode(pred)
        test_df = test_df[test_df[src] != test_df[pred]]
        score = self._score(
            torch.from_numpy(test_df.to_numpy().astype(np.float32)).to(dtype=torch.long)
        )
        result_df = test_df.loc[pd.Series(score.detach().numpy()) >= threshold]
        s, r, d = (
            test_df[src].map(self._id2node),
            test_df[rel].map(self._id2relation),
            test_df[pred].map(self._id2node),
        )
        result_df = pd.concat([s, r, d], axis=1)
        result_df.columns = [src, rel, pred]
        return result_df

    def predict_links(
        self, threshold=0.99, return_embeddings=True, retain_old_edges=False
    ):
        """predict links over entire graph given a threshold

        Args:
            threshold (float, optional): Probability threshold.
                Defaults to 0.99.
            return_embeddings (bool, optional): will return DataFrame of
                predictions and node embeddings. Defaults to True.
            retain_old_edges (bool, optional): will include old edges in
                predicted graph. Defaults to False.

        Returns:
            graph: (graphistry graph) or
                    (graphistry graph, DataFrame of predictions and
                    node embeddings) when return_embeddings=True
        """

        predicted_links, node_embeddings = self._predict(
            self.triplets, threshold, infer="all"
        )

        existing_links = self._edges[[self._source, self._relation, self._destination]]

        if retain_old_edges:
            all_links = pd.concat(
                [existing_links, predicted_links], ignore_index=True
            ).drop_duplicates()
        else:
            all_links = predicted_links

        g_new = self.nodes(self._nodes, self._node)
        g_new = g_new.edges(all_links, self._source, self._destination)

        if return_embeddings:
            return g_new, predicted_links, node_embeddings
        return g_new

    def _score(self, triplets:Union[np.ndarray, torch.Tensor]) -> torch.Tensor:
        emb = self._embeddings.clone().detach()
        if type(triplets) != torch.Tensor:
            triplets = torch.tensor(triplets)
        score = self._embed_model.score(emb, triplets)
        prob = torch.sigmoid(score)
        return prob.detach()

    def _eval(self, threshold:float) -> float:
        if self.test_idx != []:
            #from time import time
            #t = time()
            triplets = self.triplets[self.test_idx]
            score = self._score(triplets)
            score = 100 * len(score[score >= threshold]) / len(score)
            return score
        else:
            log("WARNING: train_split must be < 1 for _eval()")


class HeteroEmbed(nn.Module):
    def __init__(
        self,
        num_nodes:int,
        num_rels:int,
        d:int,
        proto:Callable[[tt, tt, tt], tt],
        node_features=None,
        device:Optional[Union[torch.device, str]]='cpu',
        reg:Optional[float]=0.01
    ):
        super().__init__()

        self.reg = reg
        self.proto = proto
        self.node_features = node_features

        if self.node_features is not None:
            self.node_features = torch.tensor(
                self.node_features.values, dtype=torch.float32
            ).to(device)
            log(f"--Using node features of shape {str(node_features.shape)}")
        hidden = None
        if node_features is not None:
            hidden = self.node_features.shape[-1]
        self.rgcn = RGCNEmbed(d, num_nodes, num_rels, hidden, device=device)
        self.relational_embedding = nn.Parameter(torch.Tensor(num_rels, d))

        nn.init.xavier_uniform_(
            self.relational_embedding, gain=nn.init.calculate_gain("relu")
        )

    def __call__(self, g:dgl.DGLHeteroGraph) -> torch.Tensor:
        # returns node embeddings
        return self.rgcn.forward(g, node_features=self.node_features)

    def score(self, node_embedding:torch.Tensor, triplets:torch.Tensor) -> torch.Tensor:
        h, r, t = triplets.T
        h, r, t = (node_embedding[h], self.relational_embedding[r], node_embedding[t])
        return self.proto(h, r, t)

    def loss(self, node_embedding:torch.Tensor, triplets:torch.Tensor, labels:torch.Tensor) -> torch.Tensor:
        score = self.score(node_embedding, triplets)

        # binary crossentropy loss
        ce_loss = F.binary_cross_entropy_with_logits(score, labels)

        # regularization loss
        ne_ = torch.mean(node_embedding.pow(2))
        re_ = torch.mean(self.relational_embedding.pow(2))
        rl = ne_ + re_
        
        return ce_loss + self.reg * rl


class SubgraphIterator:
    def __init__(self, g:dgl.DGLHeteroGraph, sample_size:int=30000, num_steps:int=1000):
        self.num_steps = num_steps
        self.sample_size = int(sample_size / 2)
        self.eids = np.arange(g.num_edges())
        self.g = g
        self.num_nodes = g.num_nodes()

    def __len__(self):
        return self.num_steps

    def __getitem__(self, i:int):
        eids = torch.from_numpy(np.random.choice(self.eids, self.sample_size))

        src, dst = self.g.find_edges(eids)
        rel = self.g.edata[dgl.ETYPE][eids].numpy()

        triplets = np.stack((src, rel, dst)).T
        samples, labels = SubgraphIterator._sample_neg(
            triplets,
            self.num_nodes,
        )

        src, rel, dst = samples.T

        # might need to add bidirectional edges
        sub_g = dgl.graph((src, dst), num_nodes=self.num_nodes)
        sub_g.edata[dgl.ETYPE] = rel
        sub_g.edata["norm"] = dgl.norm_by_dst(sub_g).unsqueeze(-1)

        return sub_g, samples, labels

    @staticmethod
    def _sample_neg(triplets:np.ndarray, num_nodes:int):
        triplets = torch.tensor(triplets)
        h, r, t = triplets.T
        h_o_t = torch.randint(high=2, size=h.size())

        random_h = torch.randint(high=num_nodes, size=h.size())
        random_t = torch.randint(high=num_nodes, size=h.size())

        neg_h = torch.where(h_o_t == 0, random_h, h)
        neg_t = torch.where(h_o_t == 1, random_t, t)
        neg_triplets = torch.stack((neg_h, r, neg_t), dim=1)

        all_triplets = torch.cat((triplets, neg_triplets), dim=0)
        labels = torch.zeros((all_triplets.size()[0]))
        labels[: triplets.shape[0]] = 1
        return all_triplets, labels
