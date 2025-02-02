import math
import sys
import os
from copy import deepcopy

import torch
from torch.nn.utils import clip_grad_norm_
from torch.cuda import amp
from tqdm import tqdm

from engines.eval_func import eval_detection, eval_search_cuhk, eval_search_prw
from utils.utils import MetricLogger, SmoothedValue, mkdir, reduce_dict, warmup_lr_scheduler, write_text

def to_device(images, targets, device):
    images = [image.to(device) for image in images]
    for t in targets:
        t["boxes"] = t["boxes"].to(device)
        t["labels"] = t["labels"].to(device)
    return images, targets

def train_one_epoch(cfg, model, optimizer, data_loader, device, epoch, scaler, tfboard=None, outsys_dir=None):
    model.train()
    metric_logger = MetricLogger(delimiter="  ", txt_dir=os.path.join(outsys_dir, 'os.txt'))
    
    
    metric_logger.add_meter("lr", SmoothedValue(window_size=1, fmt="{value:.6f}"))
    header = "Epoch: [{}]".format(epoch)
    write_text(sentence="Epoch: [{}]".format(epoch), fpath=os.path.join(outsys_dir, 'os.txt'), print_on=False)

    # warmup learning rate in the first epoch
    if epoch == 0:
        warmup_factor = 1.0 / 1000
        # FIXME: min(1000, len(data_loader) - 1)
        warmup_iters = len(data_loader) - 1
        warmup_scheduler = warmup_lr_scheduler(optimizer, warmup_iters, warmup_factor)

    for i, (images, targets) in enumerate(
            metric_logger.log_every(data_loader, cfg.DISP_PERIOD, header)
    ):
        images, targets = to_device(images, targets, device)
        optimizer.zero_grad()

        with amp.autocast():
            loss_dict = model(images, targets)
            losses = sum(loss for loss in loss_dict.values())

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = reduce_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())
        loss_value = losses_reduced.item()

        if not math.isfinite(loss_value):
            print(f"Loss is {loss_value}, stopping training")
            print(loss_dict_reduced)
            
            write_text("Loss is {}, stopping training".format(loss_value), fpath=os.path.join(outsys_dir, 'os.txt'))
            write_text(loss_dict_reduced, fpath=os.path.join(outsys_dir, 'os.txt'))
            
            sys.exit(1)

        scaler.scale(losses).backward()
        if cfg.SOLVER.CLIP_GRADIENTS > 0:
            scaler.unscale_(optimizer)
            clip_grad_norm_(model.parameters(), cfg.SOLVER.CLIP_GRADIENTS)
        scaler.step(optimizer)
        scaler.update()

        if epoch == 0:
            warmup_scheduler.step()

        metric_logger.update(loss=loss_value, **loss_dict_reduced)
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
        if tfboard:
            iter = epoch * len(data_loader) + i
            for k, v in loss_dict_reduced.items():
                tfboard.add_scalars("train", {k: v}, iter)


@torch.no_grad()
def evaluate_performance(
        model, gallery_loader, query_loader, device, use_gt=False, use_cache=False, use_cbgm=False, outsys_dir=None
):
    """
    Args:
        use_gt (bool, optional): Whether to use GT as detection results to verify the upper
                                bound of person search performance. Defaults to False.
        use_cache (bool, optional): Whether to use the cached features. Defaults to False.
        use_cbgm (bool, optional): Whether to use Context Bipartite Graph Matching algorithm.
                                Defaults to False.
    """
    
    model.eval()
    
    if use_cache:
        eval_cache = torch.load(os.path.join(outsys_dir, "eval_cache/eval_cache.pth"))
        gallery_dets = eval_cache["gallery_dets"]
        gallery_feats = eval_cache["gallery_feats"]
        query_dets = eval_cache["query_dets"]
        query_feats = eval_cache["query_feats"]
        query_box_feats = eval_cache["query_box_feats"]
    else:
        ### COMMENT START ###
        gallery_dets, gallery_feats = [], []
        for images, targets in tqdm(gallery_loader, ncols=0):
            images, targets = to_device(images, targets, device)
            if not use_gt:
                # with amp.autocast():
                outputs = model(images)
            else:
                boxes = targets[0]["boxes"]
                n_boxes = boxes.size(0)
                # with amp.autocast():
                embeddings = model(images, targets)
                outputs = [
                    {
                        "boxes": boxes,
                        "embeddings": torch.cat(embeddings),
                        "labels": torch.ones(n_boxes).to(device),
                        "scores": torch.ones(n_boxes).to(device),
                    }
                ]

            for output in outputs:
                box_w_scores = torch.cat([output["boxes"], output["scores"].unsqueeze(1)], dim=1)
                gallery_dets.append(box_w_scores.cpu().numpy())
                gallery_feats.append(output["embeddings"].cpu().numpy())

        # regarding query image as gallery to detect all people
        # i.e. query person + surrounding people (context information)
        query_dets, query_feats = [], []
        for images, targets in tqdm(query_loader, ncols=0):
            images, targets = to_device(images, targets, device)
            # targets will be modified in the model, so deepcopy it
            # with amp.autocast():
            outputs = model(images, deepcopy(targets), query_img_as_gallery=True)

            # consistency check
            gt_box = targets[0]["boxes"].squeeze()
            assert (
                gt_box - outputs[0]["boxes"][0]
            ).sum() <= 0.001, "GT box must be the first one in the detected boxes of query image"

            for output in outputs:
                box_w_scores = torch.cat([output["boxes"], output["scores"].unsqueeze(1)], dim=1)
                query_dets.append(box_w_scores.cpu().numpy())
                query_feats.append(output["embeddings"].cpu().numpy())
        ### COMMENT END ###
        
        # extract the features of query boxes
        query_box_feats = []
        for images, targets in tqdm(query_loader, ncols=0):
            images, targets = to_device(images, targets, device)
            
            embeddings = model(images, targets)
            assert len(embeddings) == 1, "batch size in test phase should be 1"
            query_box_feats.append(embeddings[0].cpu().numpy())

        cache_dir = os.path.join(outsys_dir, "eval_cache")
        mkdir(cache_dir)
        save_dict = {
            "gallery_dets": gallery_dets,
            "gallery_feats": gallery_feats,
            "query_dets": query_dets,
            "query_feats": query_feats,
            "query_box_feats": query_box_feats,
        }
        torch.save(save_dict, os.path.join(cache_dir, "eval_cache.pth"))

    if not use_gt:
        eval_detection(gallery_loader.dataset, gallery_dets, det_thresh=0.01, outsys_dir=outsys_dir)
    
    eval_search_func = (
        eval_search_cuhk if gallery_loader.dataset.name == "CUHK-SYSU" else eval_search_prw
    )
    
    ret = eval_search_func(
        gallery_loader.dataset,
        query_loader.dataset,
        gallery_dets,
        gallery_feats,
        query_box_feats,
        query_dets,
        query_feats,
        cbgm=use_cbgm,
        outsys_dir=outsys_dir,
    )

    return ret