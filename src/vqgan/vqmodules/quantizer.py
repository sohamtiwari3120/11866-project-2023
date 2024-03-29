## Code adapted from [Esser, Rombach 2021]: https://compvis.github.io/taming-transformers/

import torch
import torch.nn as nn

class VectorQuantizer(nn.Module):
    """
    see https://github.com/MishaLaskin/vqvae/blob/d761a999e2267766400dc646d82d3ac3657771d4/models/quantizer.py
    ____________________________________________
    Discretization bottleneck part of the VQ-VAE.
    Inputs:
    - n_e : number of embeddings
    - e_dim : dimension of embedding
    - beta : commitment cost used in loss term, beta * ||z_e(x)-sg[e]||^2
    _____________________________________________
    """

    def __init__(self, n_e, e_dim, beta):
        super(VectorQuantizer, self).__init__()
        self.n_e = n_e
        self.e_dim = e_dim
        self.beta = beta

        self.embedding = nn.Embedding(self.n_e, self.e_dim)
        self.embedding.weight.data.uniform_(-1.0 / self.n_e, 1.0 / self.n_e)

    def forward(self, z):
        """
        Inputs the output of the encoder network z and maps it to a discrete
        one-hot vector that is the index of the closest embedding vector e_j
        z (continuous) -> z_q (discrete)
        z.shape = (batch, channel, height, width)
        quantization pipeline:
            1. get encoder input (B,C,H,W)
            2. flatten input to (B*H*W,C)
        """
        # reshape z -> (batch, height, width, channel) and flatten
        #print('zshape', z.shape)
        z = z.permute(0, 2, 1).contiguous()
        z_flattened = z.view(-1, self.e_dim)
        # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z

        d = torch.sum(z_flattened ** 2, dim=1, keepdim=True) + \
            torch.sum(self.embedding.weight**2, dim=1) - 2 * \
            torch.matmul(z_flattened, self.embedding.weight.t())

        ## could possible replace this here
        # #\start...
        # find closest encodings
        min_encoding_indices = torch.argmin(d, dim=1).unsqueeze(1)

        min_encodings = torch.zeros(
            min_encoding_indices.shape[0], self.n_e).to(z)
        min_encodings.scatter_(1, min_encoding_indices, 1)

        # dtype min encodings: torch.float32
        # min_encodings shape: torch.Size([2048, 512])
        # min_encoding_indices.shape: torch.Size([2048, 1])

        # get quantized latent vectors
        z_q = torch.matmul(min_encodings, self.embedding.weight).view(z.shape)
        #.........\end

        # with:
        # .........\start
        #min_encoding_indices = torch.argmin(d, dim=1)
        #z_q = self.embedding(min_encoding_indices)
        # ......\end......... (TODO)

        # compute loss for embedding
        loss = self.beta * torch.mean((z_q.detach()-z)**2) + \
                   torch.mean((z_q - z.detach()) ** 2)
        #loss = torch.mean((z_q.detach()-z)**2) + self.beta * \
        #    torch.mean((z_q - z.detach()) ** 2)

        # preserve gradients
        z_q = z + (z_q - z).detach()

        # perplexity
        e_mean = torch.mean(min_encodings, dim=0)
        perplexity = torch.exp(-torch.sum(e_mean * torch.log(e_mean + 1e-10)))

        # reshape back to match original input shape
        z_q = z_q.permute(0, 2, 1).contiguous()

        return z_q, loss, (perplexity, min_encodings, min_encoding_indices)

    def get_distance(self, z):
        z = z.permute(0, 2, 1).contiguous()
        z_flattened = z.view(-1, self.e_dim)
        # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z

        d = torch.sum(z_flattened ** 2, dim=1, keepdim=True) + \
            torch.sum(self.embedding.weight**2, dim=1) - 2 * \
            torch.matmul(z_flattened, self.embedding.weight.t())
        d = torch.reshape(d, (z.shape[0], -1, z.shape[2])).permute(0,2,1).contiguous()
        return d

    def get_codebook_entry(self, indices, shape):
        # shape specifying (batch, height, width, channel)
        # TODO: check for more easy handling with nn.Embedding
        min_encodings = torch.zeros(indices.shape[0], self.n_e).to(indices)
        min_encodings.scatter_(1, indices[:,None], 1)

        # get quantized latent vectors
        #print(min_encodings.shape, self.embedding.weight.shape)
        z_q = torch.matmul(min_encodings.float(), self.embedding.weight)

        if shape is not None:
            z_q = z_q.view(shape)

            # reshape back to match original input shape
            #z_q = z_q.permute(0, 3, 1, 2).contiguous()

        return z_q


class StyleTransferVectorQuantizer(VectorQuantizer):
    """To be only used when have access to pretrained_codebook. Meant to freeze all the pretrained codebook layers

    Args:
        VectorQuantizer (_type_): _description_
    """
    def __init__(self, n_e, e_dim, beta, init_strat='normal'):
        super(StyleTransferVectorQuantizer, self).__init__(n_e, e_dim, beta)
        # print(f"Initial model weights: {self.embedding.weight}")

        # froze original codebook
        # self.style_transfer_layer = nn.Linear(1, e_dim)
        # self.style_transfer_layer = nn.Linear(1, (n_e) * e_dim)
        self.positive_param = torch.Tensor(n_e, e_dim)
        self.negative_param = torch.Tensor(n_e, e_dim)

        self.init_strat = init_strat
        if self.init_strat == 'normal':
            nn.init.normal_(self.positive_param)
            nn.init.normal_(self.negative_param)

        self.positive_style = nn.Parameter(self.positive_param)
        self.negative_style = nn.Parameter(self.negative_param)


        # torch.nn.init.constant_(self.style_transfer_layer.weight, 1)
        # torch.nn.init.constant_(self.style_transfer_layer.bias, 0)

    def freeze_codebook(self):
        self.embedding.weight.requires_grad = False

    def load_pretrained_codebook_weights(self, load_path, freeze_codebook=False):
        """To extract the pretrained codebook weights from the saved checkpoint for the VQModelTransformer which contains the modules - encoder, quantize and decoder

        Args:
            load_path (str): path to the saved VQModelTransformer checkpoint
        """
        loaded_state = torch.load(load_path,
                                  map_location=lambda storage, loc: storage)
        self.load_state_dict({'embedding.weight': loaded_state['state_dict']['module.quantize.embedding.weight']}, strict=False)
        del loaded_state
        if freeze_codebook:
            self.freeze_codebook()

    def forward(self, z, style_token):
        # style_token is a constant [0, 1]
        # style_token_emb = style_token * self.positive_style + (1-style_token) * self.negative_style
        # Get style token embeddings in the shape of the codebook
        style_token_emb = self.get_style_token_embedding(style_token)
        # generate new embeddings layer from frozen codebook and the style token emb
        new_embedding_weights = self.embedding.weight * style_token_emb
        # generating quantized
        z = z.permute(0, 2, 1).contiguous()
        z_flattened = z.view(-1, self.e_dim)
        # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z

        d = torch.sum(z_flattened ** 2, dim=1, keepdim=True) + \
            torch.sum(new_embedding_weights**2, dim=1) - 2 * \
            torch.matmul(z_flattened, new_embedding_weights.t())

        ## could possible replace this here
        # #\start...
        # find closest encodings
        min_encoding_indices = torch.argmin(d, dim=1).unsqueeze(1)

        min_encodings = torch.zeros(
            min_encoding_indices.shape[0], self.n_e).to(z)
        min_encodings.scatter_(1, min_encoding_indices, 1)

        # dtype min encodings: torch.float32
        # min_encodings shape: torch.Size([2048, 512])
        # min_encoding_indices.shape: torch.Size([2048, 1])

        # get quantized latent vectors
        z_q = torch.matmul(min_encodings, new_embedding_weights).view(z.shape)
        #.........\end

        # with:
        # .........\start
        #min_encoding_indices = torch.argmin(d, dim=1)
        #z_q = self.embedding(min_encoding_indices)
        # ......\end......... (TODO)

        # compute loss for embedding
        loss = self.beta * torch.mean((z_q.detach()-z)**2) + \
                   torch.mean((z_q - z.detach()) ** 2)
        #loss = torch.mean((z_q.detach()-z)**2) + self.beta * \
        #    torch.mean((z_q - z.detach()) ** 2)

        # preserve gradients
        z_q = z + (z_q - z).detach()

        # perplexity
        e_mean = torch.mean(min_encodings, dim=0)
        perplexity = torch.exp(-torch.sum(e_mean * torch.log(e_mean + 1e-10)))

        # reshape back to match original input shape
        z_q = z_q.permute(0, 2, 1).contiguous()

        return z_q, loss, (perplexity, min_encodings, min_encoding_indices)

    def get_style_token_embedding(self, style_token):
        # generate style token embedding
        assert style_token.shape == (1, 1)
        # style_token_emb = self.style_transfer_layer(style_token)
        # reshape the style token embedding to match the shape of the codebook
        # style_token_emb = style_token_emb.view(self.n_e, self.e_dim)
        style_token_emb = style_token * self.positive_style + (1-style_token) * self.negative_style
        return style_token_emb

    def get_distance(self, z, style_token):
        style_token_emb = self.get_style_token_embedding(style_token) # shape (n_e, e_dim), same as the codebook
        z = z.permute(0, 2, 1).contiguous()
        z_flattened = z.view(-1, self.e_dim)
        # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z

        # modify the codebook using the style token
        new_embedding_weights = self.embedding.weight * style_token_emb

        d = torch.sum(z_flattened ** 2, dim=1, keepdim=True) + \
            torch.sum(new_embedding_weights**2, dim=1) - 2 * \
            torch.matmul(z_flattened, new_embedding_weights.t())
        d = torch.reshape(d, (z.shape[0], -1, z.shape[2])).permute(0,2,1).contiguous()
        return d

    def get_codebook_entry(self, indices, shape, style_token):
        style_token_emb = self.get_style_token_embedding(style_token) # shape (n_e, e_dim), same as the codebook
        # shape specifying (batch, height, width, channel)
        # TODO: check for more easy handling with nn.Embedding
        min_encodings = torch.zeros(indices.shape[0], self.n_e).to(indices)
        min_encodings.scatter_(1, indices[:,None], 1)

        # modify the codebook using the style token
        new_embedding_weights = self.embedding.weight * style_token_emb

        # get quantized latent vectors
        #print(min_encodings.shape, self.embedding.weight.shape)
        z_q = torch.matmul(min_encodings.float(), new_embedding_weights)

        if shape is not None:
            z_q = z_q.view(shape)

            # reshape back to match original input shape
            #z_q = z_q.permute(0, 3, 1, 2).contiguous()

        return z_q

if __name__ == "__main__":
    model = StyleTransferVectorQuantizer(200, 256, 0.25)
    load_path = "/home/ubuntu/learning2listen/src/vqgan/models/l2_32_smoothSS_er2er_best.pth"
    model.load_pretrained_codebook_weights(load_path, freeze_codebook=True)
    # print(f"Model weights after loading: {model.embedding.weight} {model.embedding.weight.requires_grad}")
    z = torch.randn(1, 4, 128)
    style_token = torch.ones(1)
    z_q, loss, x = model(z, style_token)
    # print(z_q.shape, loss, len(x))
