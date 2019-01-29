
import torch
import torch.nn as nn
import torchvision.transforms as transforms
import os
import numpy as np
import pandas as pd

from sgan.utils import get_dset_group_name, get_dset_name
from sgan.physical_attention import Attention_Encoder, Attention_Decoder
import matplotlib.pyplot as plt

import matplotlib.cm as cm
import skimage.transform
from PIL import Image
from datasets.calculate_static_scene_boundaries import get_pixels_from_world


def make_mlp(dim_list, activation='relu', batch_norm=True, dropout=0):
    layers = []
    for dim_in, dim_out in zip(dim_list[:-1], dim_list[1:]):
        layers.append(nn.Linear(dim_in, dim_out))
        if batch_norm:
            layers.append(nn.BatchNorm1d(dim_out))
        if activation == 'relu':
            layers.append(nn.ReLU())
        elif activation == 'leakyrelu':
            layers.append(nn.LeakyReLU())
        if dropout > 0:
            layers.append(nn.Dropout(p=dropout))
    return nn.Sequential(*layers)


class PoolHiddenNet(nn.Module):
    """Pooling module as proposed in our paper"""
    def __init__(
        self, embedding_dim=64, h_dim=64, mlp_dim=1024, bottleneck_dim=1024,
        activation='relu', batch_norm=True, dropout=0.0, pooling_dim=2, neighborhood_size=2.0, pool_every=False
    ):
        super(PoolHiddenNet, self).__init__()

        self.mlp_dim = mlp_dim
        self.h_dim = h_dim
        self.bottleneck_dim = bottleneck_dim
        self.embedding_dim = embedding_dim
        self.pooling_dim = pooling_dim
        self.neighborhood_size = neighborhood_size

        mlp_pre_dim = embedding_dim + h_dim

        mlp_pre_pool_dims = [mlp_pre_dim, self.mlp_dim * 8, bottleneck_dim]
        self.spatial_embedding = nn.Linear(pooling_dim, embedding_dim)
        self.mlp_pre_pool = make_mlp(
            mlp_pre_pool_dims,
            activation=activation,
            batch_norm=batch_norm,
            dropout=dropout)

    def repeat(self, tensor, num_reps):
        """
        Inputs:
        -tensor: 2D tensor of any shape
        -num_reps: Number of times to repeat each row
        Outpus:
        -repeat_tensor: Repeat each row such that: R1, R1, R2, R2
        """
        col_len = tensor.size(1)
        tensor = tensor.unsqueeze(dim=1).repeat(1, num_reps, 1)
        tensor = tensor.view(-1, col_len)
        return tensor


    def forward(self, h_states, seq_start_end, end_pos, rel_pos):
        """
        Inputs:
        - h_states: Tensor of shape (num_layers, batch, h_dim)
        - seq_start_end: A list of tuples which delimit sequences within batch
        - end_pos: Tensor of shape (batch, 2)
        Output:
        - pool_h: Tensor of shape (batch, bottleneck_dim)
        """
        pool_h = []
        for _, (start, end) in enumerate(seq_start_end):
            start = start.item()
            end = end.item()
            num_ped = end - start
            curr_hidden = h_states.view(-1, self.h_dim)[start:end] # view [1, 540, 64] --> [540, 64] take start:end ped
            curr_end_pos = end_pos[start:end]

            # Repeat position -> P1, P2, P1, P2
            curr_end_pos_1 = curr_end_pos.repeat(num_ped, 1)
            # Repeat position -> P1, P1, P2, P2
            curr_end_pos_2 = self.repeat(curr_end_pos, num_ped)
            curr_rel_pos = curr_end_pos_1 - curr_end_pos_2

            curr_rel_pos[curr_rel_pos > self.neighborhood_size / 2] = self.neighborhood_size / 2
            curr_rel_pos[curr_rel_pos < -self.neighborhood_size / 2] = -self.neighborhood_size / 2
            curr_rel_pos /= (self.neighborhood_size / 2)

            if self.pooling_dim == 4:
                curr_disp = rel_pos[start:end]
                curr_disp_1 = curr_disp.repeat(num_ped, 1)
                # Repeat position -> P1, P1, P2, P2
                curr_disp_2 = self.repeat(curr_disp, num_ped)
                curr_rel_disp = curr_disp_1 - curr_disp_2
                curr_rel_pos = torch.cat([curr_rel_pos, curr_rel_disp], dim=1)

            curr_rel_embedding = self.spatial_embedding(curr_rel_pos)
            # Repeat -> H1, H2, H1, H2
            curr_hidden_1 = curr_hidden.repeat(num_ped, 1)
            mlp_h_input = torch.cat([curr_rel_embedding, curr_hidden_1], dim=1)
            curr_pool_h = self.mlp_pre_pool(mlp_h_input)
            curr_pool_h = curr_pool_h.view(num_ped, num_ped, -1).max(1)[0]
            pool_h.append(curr_pool_h) # append for all sequences the hiddens (num_ped_per_seq, 64)
        pool_h = torch.cat(pool_h, dim=0)
        return pool_h


class PhysicalPooling(nn.Module):
    def __init__(
        self, embedding_dim=64, h_dim=64, mlp_dim=1024, bottleneck_dim=1024,
        activation='relu', batch_norm=True, dropout=0.0, num_cells=15, neighborhood_size=2.0,
        pool_static_type='random', down_samples=200
    ):
        super(PhysicalPooling, self).__init__()

        self.mlp_dim = mlp_dim
        self.h_dim = h_dim
        self.bottleneck_dim = bottleneck_dim
        self.embedding_dim = embedding_dim
        self.num_cells = num_cells
        self.neighborhood_size = neighborhood_size
        self.down_samples = down_samples
        self.pool_static_type = pool_static_type

        mlp_pre_dim = embedding_dim + h_dim
        mlp_pre_pool_dims = [mlp_pre_dim, self.mlp_dim * 8, bottleneck_dim]

        if self.pool_static_type == 'random_cnn':
            self.spatial_embedding = nn.Sequential(
                nn.Conv1d(1, 1, kernel_size=(self.down_samples // self.embedding_dim, 2), stride=self.down_samples // self.embedding_dim),
                nn.LeakyReLU()
            ).cuda()

        elif self.pool_static_type == 'random_cnn_atrous':
            self.spatial_embedding = nn.Sequential(
                nn.Conv1d(1, 1, kernel_size=(self.down_samples // self.embedding_dim, 2), stride=1, dilation=(self.embedding_dim, 1)),
                nn.LeakyReLU()
            ).cuda()

        elif self.down_samples != -1 and self.pool_static_type == 'random':
            self.spatial_embedding = nn.Linear(2 * self.down_samples, embedding_dim)

        elif self.pool_static_type == 'physical_attention':
            # Pixel values must be in the range [0,1] and we must then normalize the image by the mean and standard deviation
            # of the ImageNet images' RGB channels (the resnet has been pretrained on ImageNet).
            normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            self.transform = transforms.Compose([normalize])

            self.encoder_dim = 5
            self.decoder_dim = h_dim
            self.attention_dim = bottleneck_dim
            self.embed_dim = 4
            self.encoded_image_size = 32
            #self.attention_encoder = Attention_Encoder(self.encoded_image_size)    # encoder to prepare the input for the attention module
            self.attention_decoder = Attention_Decoder(self.attention_dim, self.embed_dim, self.decoder_dim, self.encoder_dim, self.encoded_image_size)

        else:
            self.spatial_embedding = nn.Linear(2 * self.num_cells, embedding_dim)
            self.mlp_pre_pool = make_mlp(mlp_pre_pool_dims, activation=activation, batch_norm=batch_norm, dropout=dropout)

        self.scene_information = {}


    def get_map(self, dset, down_sampling=True):
        _dir = os.path.dirname(os.path.realpath(__file__))
        _dir = _dir.split("/")[:-1]
        _dir = "/".join(_dir)
        directory = _dir + '/datasets/safegan_dataset/'
        path_group = os.path.join(directory, get_dset_group_name(dset))
        path = os.path.join(path_group, dset)
        map = np.load(path + "/world_points_boundary.npy")
        if down_sampling and map.shape[0] > self.down_samples and self.down_samples != -1:
            down_sampling = (map.shape[0] // self.down_samples)
            sampled = map[::down_sampling]
            return sampled[:self.down_samples]
        else:
            return map

    def get_scene_image(self, dset):
        _dir = os.path.dirname(os.path.realpath(__file__))
        _dir = _dir.split("/")[:-1]
        _dir = "/".join(_dir)
        directory = _dir + '/datasets/safegan_dataset/'
        path_group = os.path.join(directory, get_dset_group_name(dset))
        path = os.path.join(path_group+"/segmented_scenes", dset)
        image = plt.imread(path + ".jpg")
        return image

    def get_segmentation_features(self, dset):
        _dir = os.path.dirname(os.path.realpath(__file__))
        _dir = _dir.split("/")[:-1]
        _dir = "/".join(_dir)
        directory = _dir + '/datasets/safegan_dataset/'
        path_group = os.path.join(directory, get_dset_group_name(dset))
        path = os.path.join(path_group+"/segmented_features", dset)
        features = np.load(path + "_segmentation_features.npy")
        return features

    def set_dset_list(self, data_dir):
        self.list_data_files = sorted([get_dset_name(os.path.join(data_dir, _path).split("/")[-1]) for _path in os.listdir(data_dir)])
        for name in self.list_data_files:
            if self.pool_static_type == "physical_attention":
                '''image = self.get_scene_image(name)
                image = torch.from_numpy(image).type(torch.float).cuda()
                # Images fed to the model must be a Float tensor of dimension N, 3, 256, 256, where N is the batch size.
                # PyTorch follows the NCHW convention, which means the channels dimension (C) must precede the size dimensions
                image = image.permute(2, 0, 1)
                # Normalize the image
                image = self.transform(image)
                self.scene_information[name] = self.attention_encoder(image.unsqueeze(0))'''
                features = self.get_segmentation_features(name)
                features = torch.from_numpy(features).type(torch.float).cuda()
                self.scene_information[name] = features
            else:
                map = self.get_map(name)
                map = torch.from_numpy(map).type(torch.float).cuda()
                self.scene_information[name] = map

    def repeat(self, tensor, num_reps):
        """
        Inputs:
        -tensor: 2D tensor of any shape
        -num_reps: Number of times to repeat each row
        Outpus:
        -repeat_tensor: Repeat each row such that: R1, R1, R2, R2
        """
        col_len = tensor.size(1)
        tensor = tensor.unsqueeze(dim=1).repeat(1, num_reps, 1)
        tensor = tensor.view(-1, col_len)
        return tensor

    def check(self, curr_end_pos, annotated_points):
        import matplotlib.pyplot as plt
        plt.scatter(annotated_points[:, 0], annotated_points[:, 1])
        plt.scatter(curr_end_pos[0], curr_end_pos[1])
        plt.axis('equal')
        plt.show()

    def get_raycast_grid_points(self, ped_positions, boundary_points, num_rays, radius, return_true_points=False):
        # It returns num_beams boundary points for each pedestrian
        # All inputs are not repeated
        if num_rays == 0:
            print("The number of rays should be > 0!")
            return None
        round_decimal_digit = 2
        ped_positions = ped_positions.detach()

        ped_ids = torch.from_numpy(np.arange(ped_positions.size(0))).unsqueeze(1).cuda()
        ped_ids_repeated = self.repeat(ped_ids, boundary_points.size(0))
        ped_positions_repeated = self.repeat(ped_positions, boundary_points.size(0))
        boundary_points_repeated = boundary_points.repeat(ped_positions.size(0), 1)

        # Compute the polar coordinates of the boundary points (thetas and radiuses), considering as origin the current pedestrian position
        boundary_points_repeated_polar = boundary_points_repeated - ped_positions_repeated      # Coordinates after considering as origin the current pedestrian position
        radiuses_boundary_points = torch.norm(boundary_points_repeated_polar, dim=1)
        # I round the theta values otherwise I will never take the boundary points because they can have a difference in the last digits
        # (eg. 3.14159 is considered different from the possible ray angle of 3.14158). It would be difficult to find points that have the exact same angle of the rays.
        thetas_boundary_points = torch.round( torch.atan2(boundary_points_repeated_polar[:, 1], boundary_points_repeated_polar[:, 0]) * torch.tensor( 10^round_decimal_digit ).float().cuda())\
                                 / torch.tensor( 10^round_decimal_digit ).float().cuda()

        # Build Dataframe with [pedestrians_ids, thetas_boundaries, radiuses_boundaries]
        df = pd.DataFrame(columns=['ped_id', 'theta_boundary', 'radius_boundary'],
                          data=np.concatenate((ped_ids_repeated, thetas_boundary_points.view(-1, 1), radiuses_boundary_points.view(-1, 1)), axis=1))

        if not return_true_points:
            # Compute the angles of the rays and add "num_rays" points on these rays at a distance of "radius" so that there will be always "num_rays" points as output
            rays_angles = torch.tensor(np.round( np.linspace(-np.pi, np.pi - ((2 * np.pi) / num_rays), num_rays), round_decimal_digit )).unsqueeze(1).cuda()
            rays_angles_repeated = rays_angles.repeat(ped_positions.size(0), 1)
            # Add these points to the boundary points dataframe
            df_new_points = pd.DataFrame(columns=['ped_id', 'theta_boundary', 'radius_boundary'],
                                         data=np.concatenate((self.repeat(torch.from_numpy(np.arange(ped_positions.size(0))).unsqueeze(1), rays_angles.size(0)),
                                                             rays_angles_repeated,
                                                             torch.tensor([radius] * rays_angles_repeated.size(0)).unsqueeze(1)), axis=1))
            df = df.append(df_new_points, ignore_index=True)
        else:
            # Select only the points that are in the range of 0-"radius" meters
            df = df.loc[df['radius_boundary'] <= radius]
            # Create a new dataframe with "num_beams" points at a distance of 0 meter from the curr pedestrian position, so that afterwards
            # they will be output in the case in some beams there are no points in the range 0-"radius" meters
            rays_angles = torch.tensor(np.round(np.linspace(-np.pi, np.pi - ((2 * np.pi) / num_rays), num_rays), round_decimal_digit)).unsqueeze(1).cuda()
            rays_angles_repeated = rays_angles.repeat(ped_positions.size(0), 1)
            # Add these points to the boundary points dataframe
            df_new_points = pd.DataFrame(columns=['ped_id', 'theta_boundary', 'radius_boundary'],
                                         data=np.concatenate((self.repeat(torch.from_numpy(np.arange(ped_positions.size(0))).unsqueeze(1), rays_angles.size(0)),
                                                              rays_angles_repeated,
                                                              torch.tensor([0] * rays_angles_repeated.size(0)).unsqueeze(1)), axis=1))

        # Select only the points ON he rays
        df_selected = df.loc[df['theta_boundary'].isin(rays_angles.cpu().numpy()[:, 0])]
        # Select the closest point on each ray
        polar_grids_points = df_selected.ix[df_selected.groupby(['ped_id', 'theta_boundary'])['radius_boundary'].idxmin()]

        if return_true_points:
            # If there are no points in the range 0-"radius" meters, return points at a distance of 0 meter from curr
            # pedestrian position, instead of returning points at "radius" meter (at the edge of the polar grid area)
            polar_grids_points = polar_grids_points.append(df_new_points, ignore_index=True)
            df_selected = polar_grids_points.loc[polar_grids_points['theta_boundary'].isin(rays_angles.cpu().numpy()[:, 0])]
            polar_grids_points = df_selected.ix[df_selected.groupby(['ped_id', 'theta_boundary'])['radius_boundary'].idxmax()]

        # Convert the chosen points from polar to cartesian coordinates
        ped_positions_repeated = self.repeat(ped_positions, num_rays)
        x_boundaries_chosen = torch.tensor(polar_grids_points['radius_boundary'].values).cuda().float() \
                                  * torch.cos(torch.tensor(polar_grids_points['theta_boundary'].values).cuda()).float() + ped_positions_repeated[:, 0]
        y_boundaries_chosen = torch.tensor(polar_grids_points['radius_boundary'].values).cuda().float() \
                                  * torch.sin(torch.tensor(polar_grids_points['theta_boundary'].values).cuda()).float() + ped_positions_repeated[:, 1]
        cartesian_grid_points = torch.stack((x_boundaries_chosen, y_boundaries_chosen)).transpose(0, 1)

        return cartesian_grid_points


    def get_polar_grid_points(self, ped_positions, ped_directions, boundary_points, num_beams, radius, return_true_points=False):
        # It returns num_beams boundary points for each pedestrian
        # All inputs are not repeated
        ped_positions = ped_positions.detach()
        ped_directions = ped_directions.detach()
        thetas_peds = torch.atan2(ped_directions[:, 1], ped_directions[:, 0]).unsqueeze(1)
        thetas_peds_repeated = self.repeat(thetas_peds, boundary_points.size(0))
        ped_positions_repeated = self.repeat(ped_positions, boundary_points.size(0))
        ped_ids = torch.from_numpy(np.arange(ped_positions.size(0))).unsqueeze(1).cuda()
        ped_ids_repeated = self.repeat(ped_ids, boundary_points.size(0))
        boundary_points_repeated = boundary_points.repeat(ped_positions.size(0), 1)

        # Compute the new coordinates with respect to the pedestrians (the origin is the pedestrian position, the positive x semiaxes correspond to the pedestrians directions)
        new_x_boundaries = (boundary_points_repeated[:, 0] - ped_positions_repeated[:, 0]) * torch.cos(thetas_peds_repeated[:, 0]) \
                           + (boundary_points_repeated[:, 1] - ped_positions_repeated[:, 1]) * torch.sin(thetas_peds_repeated[:, 0])
        new_y_boundaries = -(boundary_points_repeated[:, 0] - ped_positions_repeated[:, 0]) * torch.sin(thetas_peds_repeated[:, 0]) \
                           + (boundary_points_repeated[:, 1] - ped_positions_repeated[:, 1]) * torch.cos(thetas_peds_repeated[:, 0])
        boundary_points_repeated = torch.stack((new_x_boundaries, new_y_boundaries)).transpose(0, 1)

        # Compute polar coordinates of boundary points after conversion to the pedestrian reference systems
        radiuses_boundary_points = torch.norm(boundary_points_repeated, dim=1).unsqueeze(1)
        thetas_boundary_points = torch.atan2(boundary_points_repeated[:, 1], boundary_points_repeated[:, 0]).unsqueeze(1)

        # Build Dataframe with [pedestrians_ids, thetas_boundaries, radiuses_boundaries]
        df = pd.DataFrame(columns=['ped_id', 'theta_boundary', 'radius_boundary'],
                          data=np.concatenate( (ped_ids_repeated, thetas_boundary_points, radiuses_boundary_points), axis=1 ))

        if not return_true_points:
            # Add num_beams equidistant points for each pedestrian so that, if there are no other points in that polar grid beams, there will be always num_beams points
            thetas_new_boundaries = torch.from_numpy(np.linspace(-np.pi/2+(np.pi/num_beams)/2, np.pi/2-(np.pi/num_beams)/2, num_beams)).unsqueeze(1).cuda()
            thetas_new_boundaries_repeated = thetas_new_boundaries.repeat(ped_positions.size(0), 1)
            df_new_thetas = pd.DataFrame(columns=['ped_id', 'theta_boundary', 'radius_boundary'],
                                         data=np.concatenate( (self.repeat(torch.from_numpy(np.arange(ped_positions.size(0))).unsqueeze(1), thetas_new_boundaries.size(0))[:, 0].unsqueeze(1),
                                                               thetas_new_boundaries_repeated,
                                                               torch.tensor([radius] * thetas_new_boundaries_repeated.size(0)).unsqueeze(1)), axis=1 ))
            df = df.append(df_new_thetas, ignore_index=True)
        else:
            # Select only the points that are in the range of 0-"radius" meters
            df = df.loc[df['radius_boundary'] <= radius]
            # Create a new dataframe with "num_beams" points at a distance of 0 meter from the curr pedestrian position, so that afterwards
            # they will be output in the case in some beams there are no points in the range 0-"radius" meters
            thetas_new_boundaries = torch.from_numpy(np.linspace(-np.pi/2 + (np.pi / num_beams)/2, np.pi/2 - (np.pi / num_beams)/2, num_beams)).unsqueeze(1).cuda()
            thetas_new_boundaries_repeated = thetas_new_boundaries.repeat(ped_positions.size(0), 1)
            df_new_thetas = pd.DataFrame(columns=['ped_id', 'theta_boundary', 'radius_boundary'],
                                         data=np.concatenate((self.repeat(torch.from_numpy(np.arange(ped_positions.size(0))).unsqueeze(1), thetas_new_boundaries.size(0))[:, 0].unsqueeze(1),
                                                              thetas_new_boundaries_repeated,
                                                              torch.tensor([0] * thetas_new_boundaries_repeated.size(0)).unsqueeze(1)), axis=1))

        # Assign a categorical label to boundary points according to the bin they belong to
        df_categorized = pd.cut(df["theta_boundary"], np.linspace(-np.pi/2, np.pi/2, num_beams+1))
        # For each pedestrian and each polar grid beam, choose the closest boundary point
        polar_grids_points = df.ix[ df.groupby(['ped_id', df_categorized])['radius_boundary'].idxmin() ]

        if return_true_points:
            # If there are no points in the range 0-"radius" meters, return points at a distance of 0 meter from curr
            # pedestrian position, instead of returning points at "radius" meter (at the edge of the polar grid area)
            polar_grids_points = polar_grids_points.append(df_new_thetas, ignore_index=True)
            df_categorized = pd.cut(polar_grids_points["theta_boundary"], np.linspace(-np.pi/2, np.pi/2, num_beams+1))
            polar_grids_points = polar_grids_points.ix[ polar_grids_points.groupby(['ped_id', df_categorized])['radius_boundary'].idxmax() ]

        # Convert back the polar coordinates of the chosen boundary points in cartesian coordinates
        ped_positions_repeated = self.repeat(ped_positions, num_beams)
        thetas_peds_repeated = self.repeat(thetas_peds, num_beams)
        new_x_boundaries_chosen = torch.tensor(polar_grids_points['radius_boundary'].values).cuda().float() \
                                  * torch.cos(torch.tensor(polar_grids_points['theta_boundary'].values).cuda()).float()
        new_y_boundaries_chosen = torch.tensor(polar_grids_points['radius_boundary'].values).cuda().float() \
                                  * torch.sin(torch.tensor(polar_grids_points['theta_boundary'].values).cuda()).float()
        x_boundaries_chosen = new_x_boundaries_chosen * torch.cos(thetas_peds_repeated[:, 0]) \
                              - new_y_boundaries_chosen * torch.sin(thetas_peds_repeated[:, 0]) + ped_positions_repeated[:, 0]
        y_boundaries_chosen = new_x_boundaries_chosen * torch.sin(thetas_peds_repeated[:, 0]) \
                              + new_y_boundaries_chosen * torch.cos(thetas_peds_repeated[:, 0]) + ped_positions_repeated[:, 1]
        cartesian_grid_points = torch.stack((x_boundaries_chosen, y_boundaries_chosen)).transpose(0, 1)

        return cartesian_grid_points

    def forward(self, h_states, seq_start_end, end_pos, rel_pos, seq_scene_ids):
        """
        Inputs:
        - h_states: Tensor of shape (num_layers, batch, h_dim)
        - seq_start_end: A list of tuples which delimit sequences within batch
        - end_pos: Tensor of shape (batch, 2)
        Output:
        - pool_h: Tensor of shape (batch, bottleneck_dim)
        """

        seq_scenes = [self.list_data_files[num] for num in seq_scene_ids]
        pool_h = []
        for i, (start, end) in enumerate(seq_start_end):
            start = start.item()
            end = end.item()
            num_ped = end - start
            curr_hidden_1 = h_states.view(-1, self.h_dim)[start:end]
            curr_end_pos = end_pos[start:end]
            # If it used attention module, scene_info will contain the scene images, otherwise it will contain the boundary points
            scene_info = self.scene_information[seq_scenes[i]]

            if self.pool_static_type == "physical_attention":
                curr_disp_pos = rel_pos[start:end]
                encoder_out = scene_info.repeat(num_ped, 1, 1, 1)
                # Flatten image
                encoder_out = encoder_out.view(num_ped, -1, self.encoder_dim)  # (batch_size, num_pixels, encoder_dim)
                curr_pool_h, attention_weights = self.attention_decoder(encoder_out, curr_hidden_1, torch.cat([curr_end_pos, curr_disp_pos], dim=1))


                '''PLOT ATTENTION WEIGHTS'''
                '''if i==0 and self.giuseppe==0:
                    plt.clf()
                    image = Image.open("/home/q472489/Desktop/FLORA/code/social_gan/datasets/safegan_dataset/SDD/segmented_scenes/"+seq_scenes[i]+".jpg")
                    image = image.resize([14 * 24, 14 * 24], Image.LANCZOS)
                    plt.imshow(image)
                    h_matrix = pd.read_csv("/home/q472489/Desktop/FLORA/code/social_gan/datasets/safegan_dataset/SDD/"+ seq_scenes[i] + '/{}_homography.txt'.format(seq_scenes[i]), delim_whitespace=True, header=None).values
                    original_image_size = Image.open("/home/q472489/Desktop/FLORA/code/social_gan/datasets/safegan_dataset/SDD/"+seq_scenes[i]+"/annotated_boundaries.jpg").size
                    pixels = get_pixels_from_world(curr_end_pos, h_matrix, True)
                    pixels = pixels*(14*24/original_image_size[0], 14*24/original_image_size[1])
                    plt.scatter(pixels[:, 0], pixels[:, 1], marker='.', color="r")
                    attention_weights = attention_weights.view(-1, self.encoded_image_size, self.encoded_image_size).detach().cpu().numpy()
                    alpha = skimage.transform.pyramid_expand(attention_weights[0], upscale=24, sigma=8)
                    plt.imshow(alpha, alpha=0.7)
                    plt.set_cmap(cm.Greys_r)
                    plt.axis('off')
                    plt.show()
                    self.giuseppe += 1'''

            else:

                if "random" in self.pool_static_type:
                    self.num_cells = scene_info.size(0)

                # Repeat position -> P1, P1, P1, ....num_cells  P2, P2 #
                curr_ped_pos_repeated = self.repeat(curr_end_pos, self.num_cells)

                if "random" in self.pool_static_type:
                    boundary_points_per_ped = scene_info.repeat(num_ped, 1)
                    curr_rel_pos = boundary_points_per_ped.view(-1, 2) - curr_ped_pos_repeated
                    curr_rel_pos = torch.clamp(curr_rel_pos, -self.neighborhood_size, self.neighborhood_size)

                elif "polar" in self.pool_static_type:
                    curr_disp_pos = rel_pos[start:end]
                    boundary_points_per_ped = self.get_polar_grid_points(curr_end_pos, curr_disp_pos, scene_info, self.num_cells,
                                                                         self.neighborhood_size, return_true_points=(self.pool_static_type=="polar_true_points"))
                    curr_rel_pos = boundary_points_per_ped.view(-1, 2) - curr_ped_pos_repeated

                elif "raycast" in self.pool_static_type:
                    boundary_points_per_ped = self.get_raycast_grid_points(curr_end_pos, scene_info, self.num_cells,
                                                                           self.neighborhood_size, return_true_points=(self.pool_static_type=="raycast_true_points"))
                    curr_rel_pos = boundary_points_per_ped.view(-1, 2) - curr_ped_pos_repeated

                curr_rel_pos = torch.div(curr_rel_pos, self.neighborhood_size)


                if self.pool_static_type == "random_cnn" or self.pool_static_type == "random_cnn_atrous":
                    curr_rel_embedding = self.spatial_embedding(curr_rel_pos.view(num_ped, 1, -1, curr_rel_pos.size(1))).squeeze()
                    # Since it is not always possible to have kernel dimensions that produce exactly embedding_dim features
                    # as convolution output (it depends on the number of annotated points), I have to select only the first embedding_dim ones
                    curr_rel_embedding = curr_rel_embedding[:, :self.embedding_dim]
                else:
                    curr_rel_embedding = self.spatial_embedding(curr_rel_pos.view(num_ped, self.num_cells * curr_rel_pos.size(1)))

                mlp_h_input = torch.cat([curr_rel_embedding, curr_hidden_1], dim=1)
                curr_pool_h = self.mlp_pre_pool(mlp_h_input)


            pool_h.append(curr_pool_h) # append for all sequences the hiddens (num_ped_per_seq, 64)
        pool_h = torch.cat(pool_h, dim=0)
        return pool_h


class SocialPooling(nn.Module):
    """Current state of the art pooling mechanism:
    http://cvgl.stanford.edu/papers/CVPR16_Social_LSTM.pdf"""
    def __init__(
        self, h_dim=64, bottleneck_dim= 1024,activation='relu', batch_norm=True, dropout=0.0,
        neighborhood_size=2.0, grid_size=8, pool_dim=None
    ):
        super(SocialPooling, self).__init__()
        self.h_dim = h_dim
        self.grid_size = grid_size
        self.neighborhood_size = neighborhood_size
        if pool_dim:
            mlp_pool_dims = [grid_size * grid_size * h_dim, pool_dim]
        else:
            mlp_pool_dims = [grid_size * grid_size * h_dim, bottleneck_dim]

        self.mlp_pool = make_mlp(
            mlp_pool_dims,
            activation=activation,
            batch_norm=batch_norm,
            dropout=dropout
        )

    def get_bounds(self, ped_pos):
        top_left_x = ped_pos[:, 0] - self.neighborhood_size / 2
        top_left_y = ped_pos[:, 1] + self.neighborhood_size / 2
        bottom_right_x = ped_pos[:, 0] + self.neighborhood_size / 2
        bottom_right_y = ped_pos[:, 1] - self.neighborhood_size / 2
        top_left = torch.stack([top_left_x, top_left_y], dim=1)
        bottom_right = torch.stack([bottom_right_x, bottom_right_y], dim=1)
        return top_left, bottom_right

    def get_grid_locations(self, top_left, other_pos):
        cell_x = torch.floor(
            ((other_pos[:, 0] - top_left[:, 0]) / self.neighborhood_size) *
            self.grid_size)
        cell_y = torch.floor(
            ((top_left[:, 1] - other_pos[:, 1]) / self.neighborhood_size) *
            self.grid_size)
        grid_pos = cell_x + cell_y * self.grid_size
        return grid_pos

    def repeat(self, tensor, num_reps):
        """
        Inputs:
        -tensor: 2D tensor of any shape
        -num_reps: Number of times to repeat each row
        Outpus:
        -repeat_tensor: Repeat each row such that: R1, R1, R2, R2
        """
        col_len = tensor.size(1)
        tensor = tensor.unsqueeze(dim=1).repeat(1, num_reps, 1)
        tensor = tensor.view(-1, col_len)
        return tensor


    def forward(self, h_states, seq_start_end, end_pos, rel_pos):
        """
        Inputs:
        - h_states: Tesnsor of shape (num_layers, batch, h_dim)
        - seq_start_end: A list of tuples which delimit sequences within batch.
        - end_pos: Absolute end position of obs_traj (batch, 2)
        Output:
        - pool_h: Tensor of shape (batch, h_dim)
        """
        pool_h = []
        for _, (start, end) in enumerate(seq_start_end):
            start = start.item()
            end = end.item()
            num_ped = end - start
            grid_size = self.grid_size * self.grid_size
            curr_hidden = h_states.view(-1, self.h_dim)[start:end]
            curr_hidden_repeat = curr_hidden.repeat(num_ped, 1)
            curr_end_pos = end_pos[start:end]
            curr_pool_h_size = (num_ped * grid_size) + 1
            curr_pool_h = curr_hidden.new_zeros((curr_pool_h_size, self.h_dim))
            # curr_end_pos = curr_end_pos.data
            top_left, bottom_right = self.get_bounds(curr_end_pos)

            # Repeat position -> P1, P2, P1, P2
            curr_end_pos = curr_end_pos.repeat(num_ped, 1)
            # Repeat bounds -> B1, B1, B2, B2
            top_left = self.repeat(top_left, num_ped)
            bottom_right = self.repeat(bottom_right, num_ped)

            grid_pos = self.get_grid_locations(
                    top_left, curr_end_pos).type_as(seq_start_end)
            # Make all positions to exclude as non-zero
            # Find which peds to exclude
            x_bound = ((curr_end_pos[:, 0] >= bottom_right[:, 0]) +
                       (curr_end_pos[:, 0] <= top_left[:, 0]))
            y_bound = ((curr_end_pos[:, 1] >= top_left[:, 1]) +
                       (curr_end_pos[:, 1] <= bottom_right[:, 1]))

            within_bound = x_bound + y_bound
            within_bound[0::num_ped + 1] = 1  # Don't include the ped itself
            within_bound = within_bound.view(-1)

            # This is a tricky way to get scatter add to work. Helps me avoid a
            # for loop. Offset everything by 1. Use the initial 0 position to
            # dump all uncessary adds.
            grid_pos += 1
            total_grid_size = self.grid_size * self.grid_size
            offset = torch.arange( 0, total_grid_size * num_ped, total_grid_size ).type_as(seq_start_end)

            offset = self.repeat(offset.view(-1, 1), num_ped).view(-1)
            grid_pos += offset
            grid_pos[within_bound != 0] = 0
            grid_pos = grid_pos.view(-1, 1).expand_as(curr_hidden_repeat)

            curr_pool_h = curr_pool_h.scatter_add(0, grid_pos,
                                                  curr_hidden_repeat)
            curr_pool_h = curr_pool_h[1:]
            pool_h.append(curr_pool_h.view(num_ped, -1))

        pool_h = torch.cat(pool_h, dim=0)
        pool_h = self.mlp_pool(pool_h)
        return pool_h