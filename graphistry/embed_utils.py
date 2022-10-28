from attr import attr
from matplotlib.font_manager import X11FontDirectories
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from tqdm import trange
from .networks import RGCNEmbed
import dgl
from dgl.dataloading import GraphDataLoader
import torch.nn.functional as F

import pandas as pd

class EmbedDistScore:

    @staticmethod
    def TransE(h, r, t):
        return (h + r - t).norm(p=1, dim=1)

    @staticmethod
    def DistMult(h, r, t):
        return (h * r * t).sum(dim=1)

    @staticmethod
    def RotatE(h, r, t):
        return -(h * r - t).norm(p=1, dim=1)
 
    

class HeterographEmbedModuleMixin(nn.Module):
    def __init__(self):
        super().__init__()

        self.protocol = {
                'TransE': EmbedDistScore.TransE,
                'DistMult': EmbedDistScore.DistMult,
                'RotatE':  EmbedDistScore.RotatE
        }

    def embed(self, src, dst, relation, proto='DistMult', d=32, use_feat=True, X=None, epochs=2, batch_size=32, *args, **kwargs):
        self._src = src
        self._dst = dst
        self.relation=relation
        self._use_feat = use_feat
        if self._use_feat:
            res = self.bind() #bind the node features to the graph
            # todo decouple self from res
            self = res = res.featurize(kind="nodes", X=X, *args, **kwargs)

        if callable(proto):
            self.proto = proto
        else:
            self.proto = self.protocol[proto]

        nodes = self._nodes[self._node] #list(set(self._edges[src].tolist() + self._edges[dst].tolist()))
        relations = list(set(self._edges[relation].tolist()))
        
        # type2id 
        node2id = {n:idx for idx, n in enumerate(nodes)}
        relation2id = {r:idx for idx, r in enumerate(relations)}

        self._id2node = {idx:n for idx, n in enumerate(nodes)}
        self._id2relation = {idx:r for idx, r in enumerate(relations)}

        s, r, t = self._edges[src].tolist(), self._edges[relation].tolist(), self._edges[dst].tolist()
        triplets = [[node2id[_s], relation2id[_r], node2id[_t]] for _s, _r, _t in zip(s, r, t)]

        # temp 
        self.triplets_ = triplets

        del s, r, t
        
        num_nodes, num_rels = len(nodes), len(relations)

        s, r, t = torch.tensor(triplets).T
        g_dgl = dgl.graph((s, t), num_nodes=num_nodes)
        g_dgl.edata[dgl.ETYPE] = r
        g_dgl.edata['norm'] = dgl.norm_by_dst(g_dgl).unsqueeze(-1)

        self.g_dgl = g_dgl

        # TODO: bidirectional connection
        g_iter = SubgraphIterator(g_dgl)
        g_dataloader = GraphDataLoader(g_iter, batch_size=batch_size, collate_fn=lambda x: x[0])

        # init model and optimizer
        model = HeteroEmbed(num_nodes, num_rels, d, proto=self.proto, 
                    node_features=self._node_features)

            
        optimizer = torch.optim.Adam(model.parameters(), lr=1e-2)

        for e in range(epochs):
            print(f'Epoch {e}')
            for data in g_dataloader:
                model.train()
                g, node_ids, edges, labels = data

                emb = model(g, node_ids)
                loss = model.loss(emb, edges, labels)
                optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optimizer.step()

                print(f"loss: {loss.item()}")

        self._embed_model = model
        model.eval()
        self._embeddings = model(g_dgl, g_dgl.nodes()).detach().numpy()
        return self

    def calculate_prob(self, test_triplet, test_triplets, threshold, h_r, node_embeddings):
        # TODO: simplify
        s, r, o_ = test_triplet
        subject_relation = test_triplet[:2]
        num_entity = len(node_embeddings)

        delete_idx = torch.sum(h_r == subject_relation, dim = 1)
        delete_idx = torch.nonzero(delete_idx == 2).squeeze()
    
        delete_entity_idx = test_triplets[delete_idx, 2].view(-1).numpy()
        perturb_entity_idx = np.array(list(set(np.arange(num_entity)) - set(delete_entity_idx)))
        perturb_entity_idx = torch.from_numpy(perturb_entity_idx).squeeze()
        perturb_entity_idx = torch.cat((perturb_entity_idx, torch.unsqueeze(o_, 0)))

        o = self.proto(
                node_embeddings[s],
                self._embed_model.relational_embedding[r],
                node_embeddings[perturb_entity_idx])

        score = torch.sigmoid(o)
        return perturb_entity_idx[score > threshold]


    def predict_link(self, test_triplets, threshold=0.5, directed=True):

        test_triplets = torch.tensor(test_triplets)
        triplets = torch.tensor(self.triplets_)

        s, r, o = triplets.T
        nodes = torch.tensor(list(set(s.tolist() + o.tolist())))
        edge_index = torch.stack([s, o])

        # make graph
        g = dgl.graph((s, o), num_nodes=edge_index.max()+1)
        g.edata[dgl.ETYPE] = r
        g.edata['norm'] = dgl.norm_by_dst(g).unsqueeze(-1)
        del s, r, o

        node_embeddings = self._embed_model(g, nodes)

        h_r = triplets[:, :2]
        t_r = torch.stack((triplets[:, 2], triplets[:, 1])).transpose(0, 1)

        visited, predicted_links = {}, []
        for test_triplet in test_triplets:
            s, r, o_ = test_triplet
            k = ''.join([str(s), "_", str(r)])
            kr = ''.join([str(r), "_", str(s)])

            # for [s, r] -> {d}
            if k not in visited:

                links = self.calculate_prob(
                        test_triplet, 
                        test_triplets,
                        threshold, 
                        h_r, 
                        node_embeddings
                ) 
                visited[k] = ''
                predicted_links += [[self._id2node[s.item()], self._id2relation[r.item()], 
                    self._id2node[i.item()]] for i in links]

            # for [d, r] -> {s}
            if kr not in visited and not directed:
                links = self.calculate_prob(
                        test_triplet,
                        test_triplets,
                        threshold,
                        t_r,
                        node_embeddings
                )
                visited[k] = ''
                predicted_links += [[self._id2node[s.item()], self._id2relation[r.item()], 
                    self._id2node[i.item()]] for i in links]
                
        # TODO: dropduplicates    
        predicted_links = pd.DataFrame(
            predicted_links, columns = [self._src, self.relation, self._dst]
        )
        return predicted_links, node_embeddings

    def predict_link_all(self, threshold=0.5, return_embeddings=True):
        predicted_links, node_embeddings = self.predict_link(
                    torch.tensor(self.triplets_),
                    threshold
            )
        
        g_new = self.nodes(self._nodes).edges(predicted_links, self._src, self._dst)
        #create a new graphistry graph
        if return_embeddings:
            return g_new, predicted_links, node_embeddings
        return g_new
    
    def score(self, triplets):
        emb = torch.tensor(self._embeddings)
        triplets = torch.tensor(triplets)
        score =  self._embed_model.score(emb, triplets)
        prob = torch.sigmoid(score)
        return prob.detach().numpy()
       

class HeteroEmbed(nn.Module):
    def __init__(self, num_nodes, num_rels, d, proto, node_features=None, reg=0.01):
        super().__init__()

        self.reg = reg
        self.proto = proto
        self._node_features = node_features
        if self._node_features is not None:
            self._node_features = torch.tensor(self._node_features.values, dtype=torch.float32)
            print("node_features shape", node_features.shape)
        hidden = self._node_features.shape[-1] if node_features is not None else None
        self.rgcn = RGCNEmbed(d, num_nodes, num_rels, hidden)
        self.relational_embedding = nn.Parameter(torch.Tensor(num_rels, d))

        nn.init.xavier_uniform_(
                self.relational_embedding,
                gain=nn.init.calculate_gain('relu')
        )

    def __call__(self, g, node_ids):
        # returns node embeddings
        x = None
        if self._node_features is not None:
            #node_ids = torch.tensor([n for n in node_ids if n < len(self._node_features)])
            x = self._node_features[node_ids]
        return self.rgcn(g, node_ids, node_features=x)
    

    def score(self, node_embedding, triplets):
        h, r, t = triplets.T
        h, r, t = node_embedding[h], self.relational_embedding[r], node_embedding[t]
        return self.proto(h, r, t)

    def loss(self, node_embedding, triplets, labels):
        score = self.score(node_embedding, triplets)

        # binary crossentropy loss
        l = F.binary_cross_entropy_with_logits(score, labels)

        # regularization loss
        ne_ = torch.mean(node_embedding.pow(2))
        re_ = torch.mean(self.relational_embedding.pow(2))
        rl = ne_ + re_
        
        return l + self.reg * rl
    

class SubgraphIterator:
    def __init__(self, g, sample_size=30000, num_epochs=1000):
        self.num_epochs = num_epochs
        # TODO: raise exception -> sample size must be > 1
        self.sample_size = int(sample_size/2)
        self.eids = np.arange(g.num_edges())
        self.g = g

    def __len__(self):
        return self.num_epochs
    
    def __getitem__(self, i):
        eids = torch.from_numpy(
                np.random.choice(
                    self.eids, self.sample_size
                )
        )

        src, dst = self.g.find_edges(eids)
        rel = self.g.edata[dgl.ETYPE][eids].numpy()

        uniq_v, _ = torch.unique(torch.cat((src, dst)), return_inverse=True)
        num_nodes = len(uniq_v)

        triplets = np.stack((src, rel, dst)).T
        
        # negative sampling
        samples, labels = SubgraphIterator.sample_neg_(
                triplets, 
                num_nodes, 
                self.sample_size  # does nothing
        )

        src, rel, dst = samples.T

        # might need to add bidirectional edges
        sub_g = dgl.graph((src, dst), num_nodes=num_nodes)
        sub_g.edata[dgl.ETYPE] = rel
        sub_g.edata['norm'] = dgl.norm_by_dst(sub_g).unsqueeze(-1)
        uniq_v = uniq_v.view(-1).long()

        return sub_g, uniq_v, samples, labels

    @staticmethod
    def sample_neg_(triplets, num_nodes, sample_size):

        # TODO: remove all numpy operations

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
        labels[:triplets.shape[0]] = 1
        
        return all_triplets, labels