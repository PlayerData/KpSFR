import glob
import os
import os.path as osp
import numpy as np
from PIL import Image
import torch
from torch.utils import data
from torchvision import transforms
import matplotlib.pyplot as plt
import skimage.segmentation as ss
from typing import Optional
import utils


class MainTestDataset(data.Dataset):

    def __init__(self,
                root,
                data_type,
                mode,
                num_objects,
                noise_trans: Optional[float] = None,
                noise_rotate: Optional[float] = None,
                target_video: list = [],
                sfp_finetuned: Optional[bool] = False
                ):

        self.frame_h = 720
        self.frame_w = 1280
        self.root = root
        self.data_type = data_type
        self.mode = mode
        self.num_objects = num_objects
        # self.num_objects = 91
        self.noise_trans = noise_trans
        self.noise_rotate = noise_rotate
        self.sfp_finetuned = sfp_finetuned

        sequence_interval = '80_95'

        if self.sfp_finetuned:
            sfp_out_path = 'SingleFramePredict_finetuned_with_normalized'
        else:
            sfp_out_path = 'SingleFramePredict_with_normalized'

        self.image_path = osp.join(
            self.root, 'Dataset', sequence_interval)
        self.anno_path = osp.join(
            self.root, 'Annotations', sequence_interval)
        self.sfp_path = osp.join(
            self.root, sfp_out_path, sequence_interval)

        imgset_path = osp.join(self.root, self.data_type)
        self.videos = []
        self.num_frames = {}
        self.num_homographies = {}
        self.frames = {}
        self.homographies = {}
        self.segs = {}

        with open(imgset_path + '.txt', 'r') as lines:
            for line in lines:
                print(line)
                _video = line.rstrip('\n')

                if target_video and not _video in target_video:
                    continue

                self.videos.append(_video)
                self.num_frames[_video] =  1 
                # len(
                #     glob.glob(osp.join(self.image_path, _video, '*.jpg')))
                self.num_homographies[_video] = 1
                # len(
                #     glob.glob(osp.join(self.anno_path, _video, '*_homography.npy')))

                frames = sorted(os.listdir(
                    osp.join(self.image_path, _video)))[0:1]
                
                self.frames[_video] = frames

                homographies = sorted(os.listdir(
                    osp.join(self.anno_path, _video)))[0:1]
                self.homographies[_video] = homographies

                gt_segs = sorted(os.listdir(
                    osp.join(self.sfp_path, _video)))[0:1]
                self.segs[_video] = gt_segs

                break

        self.preprocess = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406],
                                 [0.229, 0.224, 0.225]),  # ImageNet
        ])

    def __len__(self):

        return len(self.videos)

    def __getitem__(self, index):
        _video_name = self.videos[index]
        _frames = self.frames[_video_name]
        _homographies = self.homographies[_video_name]
        _segs = self.segs[_video_name]
        info = {}
        info['num_objects'] = self.num_objects
        info['name'] = _video_name
        info['frames'] = []
        info['num_frames'] = self.num_frames[_video_name]
        info['single_frame_path'] = self.sfp_path

        template_grid = utils.gen_template_grid()  # template grid shape (91, 3)

        image_list = []
        homo_mat_list = []
        dilated_hm_list = []
        hm_list = []
        gt_seg_list = []

        for f_idx in range(self.num_frames[_video_name]):
            jpg_image = _frames[f_idx]
            npy_matrix = _homographies[f_idx]
            png_seg = _segs[f_idx]
            info['frames'].append(
                osp.join(self.image_path, _video_name, jpg_image))
            image = np.array(Image.open(
                osp.join(self.image_path, _video_name, jpg_image)))
            gt_h = np.load(osp.join(self.anno_path, _video_name, npy_matrix))

            sfp_seg = np.array(Image.open(
                osp.join(self.sfp_path, _video_name, png_seg)).convert('P'))
            gt_seg_list.append(sfp_seg)

            # warp grid shape (91, 3)
            warp_image, warp_grid, homo_mat = utils.gen_im_whole_grid(
                self.mode, image, f_idx, gt_h, template_grid, self.noise_trans, self.noise_rotate, index)

            # Each keypoints is considered as an object
            num_pts = warp_grid.shape[0]
            pil_image = Image.fromarray(warp_image)

            image_tensor = self.preprocess(pil_image)
            image_list.append(image_tensor)
            homo_mat_list.append(homo_mat)

            # By default, all keypoints belong to background
            # C*H*W, C:91, exclude background class
            heatmaps = np.zeros(
                (num_pts, self.frame_h // 4, self.frame_w // 4), dtype=np.float32)
            dilated_heatmaps = np.zeros_like(heatmaps)
            for keypts_label in range(num_pts):
                if np.isnan(warp_grid[keypts_label, 0]) and np.isnan(warp_grid[keypts_label, 1]):
                    continue
                px = np.rint(warp_grid[keypts_label, 0] / 4).astype(np.int32)
                py = np.rint(warp_grid[keypts_label, 1] / 4).astype(np.int32)
                cls = int(warp_grid[keypts_label, 2]) - 1
                if 0 <= px < (self.frame_w // 4) and 0 <= py < (self.frame_h // 4):
                    heatmaps[cls][py, px] = warp_grid[keypts_label, 2]
                    dilated_heatmaps[cls] = ss.expand_labels(
                        heatmaps[cls], distance=5)

            dilated_hm_list.append(dilated_heatmaps)
            hm_list.append(heatmaps)

        # TODO: use full gt segmentatino info, only previous for memory management
        dilated_hm_list = np.stack(
            dilated_hm_list, axis=0)  # num_frames*91*H*W
        T, CK, H, W = dilated_hm_list.shape
        hm_list = np.stack(hm_list, axis=0)

        # (CK:num_objects, T:num_frames, H:180, W:320)
        target_dilated_hm_list = torch.zeros((CK, T, H, W), dtype=torch.float16)
        print(target_dilated_hm_list.size())
        target_hm_list = torch.zeros(target_dilated_hm_list.size())
        cls_gt = torch.zeros(
            (self.num_frames[_video_name], H, W), dtype=torch.float32)
        lookup_list = []
        for f in range(self.num_frames[_video_name]):
            class_lables = np.ones(num_pts, dtype=np.float16) * -1
            # Those keypoints appears on the each frame
            labels = np.unique(dilated_hm_list[f])
            labels = labels[labels != 0]  # Remove background class
            for obj in labels:
                class_lables[int(obj) - 1] = obj

            for idx, obj in enumerate(class_lables):
                if obj != -1:
                    target_dilated_hm = dilated_hm_list[f, int(obj) - 1].copy()
                    target_dilated_hm[target_dilated_hm == obj] = 1
                    target_dilated_hm_tensor = utils.to_torch(
                        target_dilated_hm)
                    target_dilated_hm_list[int(
                        obj) - 1, f] = target_dilated_hm_tensor

                    target_hm = hm_list[f, int(obj) - 1].copy()
                    target_hm[target_hm == obj] = 1
                    target_hm_tensor = utils.to_torch(target_hm)
                    target_hm_list[int(obj) - 1, f] = target_hm_tensor

            # TODO: union of all target objects of ground truth segmentation
            for idx, obj in enumerate(class_lables):
                if obj != -1:
                    cls_gt[target_hm_list[idx] ==
                           1] = torch.tensor(obj).float()

        # TODO: use full single frame predict segmentatino info, only previous for memory management
        gt_seg_list = np.stack(gt_seg_list, axis=0)  # num_frames*H*W
        for f in range(self.num_frames[_video_name]):
            class_lables = np.ones(num_pts, dtype=np.float32) * -1
            # Those keypoints appears on the each single frame prediction
            labels = np.unique(gt_seg_list[f])
            labels = labels[labels != 0]  # Remove background class
            for obj in labels:
                class_lables[int(obj) - 1] = obj

            sfp_lookup = utils.to_torch(class_lables)

            # TODO: choose the range of classes for class conditioning
            sfp_interval = torch.ones_like(sfp_lookup) * -1
            cls_id = torch.unique(sfp_lookup)
            cls_id = cls_id[cls_id != -1]
            cls_list = torch.arange(cls_id.min(), cls_id.max() + 1)

            if cls_list.min() > 10:
                min_cls = cls_list.min()
                l1 = torch.arange(min_cls - 10, min_cls)
                cls_list = torch.cat([l1, cls_list], dim=0)

            if cls_list.max() < 81:
                max_cls = cls_list.max() + 1
                l2 = torch.arange(max_cls, max_cls + 10)
                cls_list = torch.cat([cls_list, l2], dim=0)

            for obj in cls_list:
                sfp_interval[int(obj) - 1] = obj

            lookup_list.append(sfp_interval)

        lookup_list = torch.stack(lookup_list, dim=0)  # T*CK:91
        selector_list = torch.ones_like(lookup_list, dtype=torch.float16)  # T*CK:91
        selector_list[lookup_list == -1] = 0
        
        # (num_frames, 3, 720, 1280);
        print(len(image_list))
        image_list = torch.stack(image_list, dim=0)
        homo_mat_list = np.stack(homo_mat_list, axis=0)
        # (K:num_objects, T:num_frames, C:1, H:180, W:320)
        target_dilated_hm_list = target_dilated_hm_list.unsqueeze(2)

        data = {}
        data['rgb'] = image_list
        data['target_dilated_hm'] = target_dilated_hm_list
        data['cls_gt'] = cls_gt
        data['gt_homo'] = homo_mat_list
        data['selector'] = selector_list
        data['lookup'] = lookup_list
        data['info'] = info

        return data


if __name__ == "__main__":

    main_test_loader = MainTestDataset(
        root='dataset/WorldCup_2014_2018', data_type='test', mode='test', num_objects=4)

    import shutil
    cnt = 1
    visual_dir = osp.join('visual', 'main_test')
    if osp.exists(visual_dir):
        print(f'Remove directory: {visual_dir}')
        shutil.rmtree(visual_dir)
    print(f'Create directory: {visual_dir}')
    os.makedirs(visual_dir, exist_ok=True)

    denorm = utils.UnNormalize(
        mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])

    for data in main_test_loader:
        image = data['rgb']
        mask = data['target_dilated_hm']
        cls_gt = data['cls_gt']
        # === debug ===
        print(f'number of frames: {cls_gt.shape[0]}')
        for j in range(cls_gt.shape[0]):
            print(torch.unique(cls_gt[j]))
            plt.imsave(osp.join(visual_dir, 'Video%d_Seg%03d.jpg' %
                       (cnt, j + 1)), utils.to_numpy(cls_gt[j]), vmin=0, vmax=91)
            plt.imsave(osp.join(visual_dir, 'Frame_%03d.jpg' %
                       (j + 1)), utils.im_to_numpy(denorm(image[j])))
            for i in range(91):
                if np.any(utils.to_numpy(mask[i, j, 0])):
                    plt.imsave(osp.join(visual_dir, '%d_dilated_mask_obj%d.jpg' % (
                        j + 1, i + 1)), utils.to_numpy(mask[i, j, 0]))
        cnt += 1
        assert False
        pass
