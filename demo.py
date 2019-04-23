import os
import time
import numpy as np
import torch
import subprocess
import torch.backends.cudnn as cudnn
import torch.utils.data as data

from dataset.total_text import TotalText
from network.textnet import TextNet
from util.detection import TextDetector
from util.augmentation import BaseTransform
from util.config import config as cfg, update_config, print_config
from util.misc import to_device
from util.option import BaseOptions
from util.visualize import visualize_detection
from util.misc import mkdirs
import cv2

def result2polygon(image, result, tcl_contour):
    """ convert geometric info(center_x, center_y, radii) into contours
    :param image: (np.array), input image
    :param result: (list), each with (n, 3), 3 denotes (x, y, radii)
    :param tcl_contour: (list), each with (n_points, 2)
    :return: (np.ndarray list), polygon format contours
    """
    all_conts = []
    for disk in result:
        mask = np.zeros(image.shape[:2], dtype=np.uint8)
        for x, y, r in disk:
            r = max(r, 1)
            cv2.circle(mask, (int(x), int(y)), int(r), (1), -1)
        _, conts, _ = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        all_conts += [cont[:, 0, :] for cont in conts]
    return all_conts


def rescale_result(image, contours, H, W):
    ori_H, ori_W = image.shape[:2]
    image = cv2.resize(image, (W, H))
    for cont in contours:
        cont[:, 0] = (cont[:, 0] * W / ori_W).astype(int)
        cont[:, 1] = (cont[:, 1] * H / ori_H).astype(int)
    return image, contours


def write_to_file(contours, file_path):
    """
    :param contours: [[x1, y1], [x2, y2]... [xn, yn]]
    :param file_path: target file path
    """
    # according to total-text evaluation method, output file shoud be formatted to: y0,x0, ..... yn,xn
    with open(file_path, 'w') as f:
        for cont in contours:
            cont = np.stack([cont[:, 1], cont[:, 0]], 1)
            cont = cont.flatten().astype(str).tolist()
            cont = ','.join(cont)
            f.write(cont + '\n')


def load_model(model, model_path):
    print('Loading from {}'.format(model_path))
    state_dict = torch.load(model_path)
    model.load_state_dict(state_dict['model'])


def inference(model, detector, test_loader, output_dir):

    model.eval()

    for i, (img, train_mask, tr_mask, tcl_mask, radius_map, sin_map, cos_map, meta) in enumerate(test_loader):

        img, train_mask, tr_mask, tcl_mask, radius_map, sin_map, cos_map = to_device(
            img, train_mask, tr_mask, tcl_mask, radius_map, sin_map, cos_map)
        # inference
        output = model(img)

        for idx in range(img.size(0)):
            print('detect {} / {} images: {}.'.format(i, len(test_loader), meta['image_id'][idx]))

            tr_pred = output[idx, 0:2].softmax(dim=0).data.cpu().numpy()
            tcl_pred = output[idx, 2:4].softmax(dim=0).data.cpu().numpy()
            sin_pred = output[idx, 4].data.cpu().numpy()
            cos_pred = output[idx, 5].data.cpu().numpy()
            radii_pred = output[idx, 6].data.cpu().numpy()

            # get model output
            det_result, tcl_contour = detector.detect(tr_pred, tcl_pred, sin_pred, cos_pred, radii_pred)  # (n_tcl, 3)

            # visualization
            img_show = img[idx].permute(1, 2, 0).cpu().numpy()
            img_show = ((img_show * cfg.stds + cfg.means) * 255).astype(np.uint8)
            contours = result2polygon(img_show, det_result, tcl_contour)

            pred_vis = visualize_detection(img_show, tr_pred[1], tcl_pred[1], contours)
            gt_contour = []
            for annot, n_annot in zip(meta['annotation'][idx], meta['n_annotation'][idx]):
                if n_annot.item() > 0:
                    gt_contour.append(annot[:n_annot].int().cpu().numpy())
            gt_vis = visualize_detection(img_show, tr_mask[idx].cpu().numpy(), tcl_mask[idx].cpu().numpy(), gt_contour)
            im_vis = np.concatenate([pred_vis, gt_vis], axis=0)
            path = os.path.join(cfg.vis_dir, '{}_test'.format(cfg.exp_name), meta['image_id'][idx])
            cv2.imwrite(path, im_vis)

            H, W = meta['Height'][idx].item(), meta['Width'][idx].item()
            img_show, contours = rescale_result(img_show, contours, H, W)

            # write to file
            mkdirs(output_dir)
            write_to_file(contours, os.path.join(output_dir, meta['image_id'][idx].replace('jpg', 'txt')))

def main():

    testset = TotalText(
        data_root='data/total-text',
        ignore_list=None,
        is_training=False,
        transform=BaseTransform(size=cfg.input_size, mean=cfg.means, std=cfg.stds)
    )
    test_loader = data.DataLoader(testset, batch_size=1, shuffle=False, num_workers=cfg.num_workers)

    # Model
    model = TextNet()
    model_path = os.path.join(cfg.save_dir, cfg.exp_name, \
              'textsnake_{}_{}.pth'.format(model.backbone_name, cfg.checkepoch))
    load_model(model, model_path)

    # copy to cuda
    model = model.to(cfg.device)
    if cfg.cuda:
        cudnn.benchmark = True
    detector = TextDetector(tr_thresh=cfg.tr_thresh, tcl_thresh=cfg.tcl_thresh)

    print('Start testing TextSnake.')
    output_dir = os.path.join(cfg.output_dir, cfg.exp_name)
    inference(model, detector, test_loader, output_dir)

    # compute DetEval
    print('Computing DetEval in {}/{}'.format(cfg.output_dir, cfg.exp_name))
    subprocess.call(['python', 'dataset/total_text/Evaluation_Protocol/Python_scripts/Deteval.py', args.exp_name])
    print('End.')


if __name__ == "__main__":
    # parse arguments
    option = BaseOptions()
    args = option.initialize()

    update_config(cfg, args)
    print_config(cfg)

    vis_dir = os.path.join(cfg.vis_dir, '{}_test'.format(cfg.exp_name))
    if not os.path.exists(vis_dir):
        os.mkdir(vis_dir)
    # main
    main()