import torch
from models import BaseVAE
from torch import nn
from torch.nn import functional as F
from .types_ import *
import pydiffvg

class VectorVAE(BaseVAE):


    def __init__(self,
                 in_channels: int,
                 latent_dim: int,
                 hidden_dims: List = None,
                 loss_fn: str = 'MSE',
                 imsize: int = 64,
                 paths: int = 4,
                 **kwargs) -> None:
        super(VectorVAE, self).__init__()

        self.latent_dim = latent_dim
        self.imsize = imsize
        self.paths = paths
        self.in_channels = in_channels
        self.sort_idx = None
        if loss_fn == 'BCE':
            self.loss_fn = F.binary_cross_entropy
        else:
            self.loss_fn = F.mse_loss
        modules = []
        if hidden_dims is None:
            hidden_dims = [32, 64, 128, 256, 512]

        # Build Encoder
        for h_dim in hidden_dims:
            modules.append(
                nn.Sequential(
                    nn.Conv2d(in_channels, out_channels=h_dim,
                              kernel_size= 3, stride= 2, padding  = 1),
                    # nn.BatchNorm2d(h_dim),
                    nn.LeakyReLU())
            )
            in_channels = h_dim

        self.encoder = nn.Sequential(*modules)
        outsize = int(imsize/(2**5))
        self.fc_mu = nn.Linear(hidden_dims[-1]*outsize*outsize, latent_dim)
        self.fc_var = nn.Linear(hidden_dims[-1]*outsize*outsize, latent_dim)

        xv, yv = torch.meshgrid([torch.arange(0, size, dtype=torch.float32)/size, torch.arange(0, size, dtype=torch.float32)/size])
        self.register_buffer('xv', xv)
        self.register_buffer('yv', yv)
        # Build Decoder
        self.decoder_input = nn.Linear(latent_dim, hidden_dims[-1])

        self.point_predictor = nn.Sequential(
            nn.ReLU(),
            nn.Linear(hidden_dims[-1], hidden_dims[-1]*2),
            nn.ReLU(),
            nn.Linear(hidden_dims[-1]*2, hidden_dims[-1]*3),
            nn.ReLU(),
            nn.Linear(hidden_dims[-1]*3, hidden_dims[-1]*4),
            nn.ReLU(),
            nn.Linear(hidden_dims[-1]*4, hidden_dims[-1]*4),
            nn.ReLU(),
            nn.Linear(hidden_dims[-1]*4, hidden_dims[-1]*4),
            nn.ReLU(),
            nn.Linear(hidden_dims[-1]*4, 2 * self.paths *3),
            nn.Sigmoid()  # bound spatial extent
        )

        self.render = pydiffvg.RenderFunction.apply

    def encode(self, input: Tensor) -> List[Tensor]:
        """
        Encodes the input by passing through the encoder network
        and returns the latent codes.
        :param input: (Tensor) Input tensor to encoder [N x C x H x W]
        :return: (Tensor) List of latent codes
        """
        result = self.encoder(input)
        result = torch.flatten(result, start_dim=1)

        # Split the result into mu and var components
        # of the latent Gaussian distribution
        mu = self.fc_mu(result)
        log_var = self.fc_var(result)

        return [mu, log_var]

    def decode(self, z: Tensor) -> Tensor:
        """
        Maps the given latent codes
        onto the image space.
        :param z: (Tensor) [B x D]
        :return: (Tensor) [B x C x H x W]
        """
        bs = z.shape[0]
        result = self.decoder_input(z)


        all_points = self.point_predictor(result)
        all_points = all_points.view(bs, self.paths*3, 2)

        all_points = ((all_points-0.5)*3 + 0.5)*self.imsize
        # if type(self.sort_idx) == type(None):
        #     angles = torch.atan(all_points[:,:,1]/all_points[:,:,0]).detach()
        #     self.sort_idx = torch.argsort(angles, dim=1)
        # Process the batch sequentially
        outputs = []
        for k in range(bs):
            # Get point parameters from network
            shapes = []
            shape_groups = []
            points = all_points[k].cpu()#[self.sort_idx[k]]

            color = torch.cat([torch.ones(4)])
            num_ctrl_pts = torch.zeros(self.paths, dtype=torch.int32) + 2

            path = pydiffvg.Path(
                num_control_points=num_ctrl_pts, points=points,
                is_closed=True)

            shapes.append(path)
            path_group = pydiffvg.ShapeGroup(
                shape_ids=torch.tensor([len(shapes) - 1]),
                fill_color=color,
                stroke_color=color)
            shape_groups.append(path_group)
            scene_args = pydiffvg.RenderFunction.serialize_scene( \
                self.imsize, self.imsize, shapes, shape_groups)
            out = self.render(self.imsize,  # width
                         self.imsize,  # height
                         2,  # num_samples_x
                         2,  # num_samples_y
                         102,  # seed
                         None,
                         *scene_args)
            # Rasterize

            # Torch format, discard alpha, make gray
            out = out.permute(2, 0, 1).view(4, self.imsize, self.imsize)[:3]#.mean(0, keepdim=True)

            outputs.append(out)
        output =  torch.stack(outputs).to(z.device)

        # map to [-1, 1]
        output = 1-output

        return output

    def reparameterize(self, mu: Tensor, logvar: Tensor) -> Tensor:
        """
        Reparameterization trick to sample from N(mu, var) from
        N(0,1).
        :param mu: (Tensor) Mean of the latent Gaussian [B x D]
        :param logvar: (Tensor) Standard deviation of the latent Gaussian [B x D]
        :return: (Tensor) [B x D]
        """
        std = torch.exp(0.5 * logvar)
        eps = torch.randn_like(std)
        return eps * std + mu

    def forward(self, input: Tensor, **kwargs) -> List[Tensor]:
        mu, log_var = self.encode(input)
        z = self.reparameterize(mu, log_var)
        return  [self.decode(z), input, mu, log_var]

    def bilinear_downsample(self, tensor, size):
        return torch.nn.functional.interpolate(tensor, size, mode='bilinear')

    def loss_function(self,
                      *args,
                      **kwargs) -> dict:
        """
        Computes the VAE loss function.
        KL(N(\mu, \sigma), N(0, 1)) = \log \frac{1}{\sigma} + \frac{\sigma^2 + \mu^2}{2} - \frac{1}{2}
        :param args:
        :param kwargs:
        :return:
        """
        recons = args[0]
        input = args[1]
        mu = args[2]
        log_var = args[3]

        kld_weight = kwargs['M_N'] # Account for the minibatch samples from the dataset
        # recons_loss =F.mse_loss(recons, input)
        # recons = (recons+1)/2
        # input = (input+1)/2
        recon_loss =self.loss_fn(recons, input)
        x, y = recons.shape[2], recons.shape[3]
        for j in range(2,3):
            recon_loss = recon_loss+self.loss_fn(self.bilinear_downsample(recons, [int(x*1.5/j), int(y*1.5/j)]),
                                                  self.bilinear_downsample(input, [int(x*1.5/j), int(y*1.5/j)]))

        kld_loss = torch.mean(-0.5 * torch.sum(1 + log_var - mu ** 2 - log_var.exp(), dim = 1), dim = 0)

        loss = recon_loss + kld_weight * kld_loss
        return {'loss': loss, 'Reconstruction_Loss':recon_loss, 'KLD':-kld_loss}

    def sample(self,
               num_samples:int,
               current_device: int, **kwargs) -> Tensor:
        """
        Samples from the latent space and return the corresponding
        image space map.
        :param num_samples: (Int) Number of samples
        :param current_device: (Int) Device to run the model
        :return: (Tensor)
        """
        z = torch.randn(num_samples,
                        self.latent_dim)

        z = z.to(current_device)

        samples = self.decode(z)
        return samples

    def generate(self, x: Tensor, **kwargs) -> Tensor:
        """
        Given an input image x, returns the reconstructed image
        :param x: (Tensor) [B x C x H x W]
        :return: (Tensor) [B x C x H x W]
        """

        return self.forward(x)[0]
 # .type(torch.FloatTensor).to(device)

    def interpolate(self, x: Tensor, **kwargs) -> Tensor:
        """
        Given an input image x, returns the reconstructed image
        :param x: (Tensor) [B x C x H x W]
        :return: (Tensor) [B x C x H x W]
        """
        mu, log_var = self.encode(x)
        all_interpolations = []
        for i in range(mu.shape[0]):
            z = self.interpolate_vectors(mu[1], mu[i], 10)
            all_interpolations.append(self.decode(z))
        return all_interpolations