import argparse
import datetime
import json
import os
import time
from bisect import bisect

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import yaml
from torch.optim import lr_scheduler
from torch.utils.tensorboard import SummaryWriter
from torch_geometric.data import DataLoader
from torch_geometric.datasets import QM9

from cgcnn.datasets import UlissigroupCO, XieGrossmanMatProj
from cgcnn.meter import AverageMeter, mae, mae_ratio
from cgcnn.models import CGCNN
from cgcnn.normalizer import Normalizer
from cgcnn.utils import save_checkpoint

parser = argparse.ArgumentParser(
    description="Graph Neural Networks for Chemistry"
)
parser.add_argument(
    "--config-yml",
    default="configs/ulissigroup_co/cgcnn.yml",
    help="Path to a config file listing data, model, optim parameters.",
)
parser.add_argument(
    "--identifier",
    default="",
    help="Experiment identifier to append to checkpoint/log/result directory",
)
parser.add_argument(
    "--num-workers",
    default=0,
    type=int,
    help="Number of dataloader workers (default: 0 i.e. use main proc)",
)
parser.add_argument(
    "--print-every",
    default=10,
    type=int,
    help="Log every N iterations (default: 10)",
)
parser.add_argument(
    "--seed", default=0, type=int, help="Seed for torch, cuda, numpy"
)

# =============================================================================
#   INPUT ARGUMENTS AND CONFIG
# =============================================================================

args = parser.parse_args()

# https://pytorch.org/docs/stable/notes/randomness.html
np.random.seed(args.seed)
torch.manual_seed(args.seed)
torch.cuda.manual_seed_all(args.seed)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

config = yaml.safe_load(open(args.config_yml, "r"))

includes = config.get("includes", [])
if not isinstance(includes, list):
    raise AttributeError(
        "Includes must be a list, {} provided".format(type(includes))
    )

for include in includes:
    include_config = yaml.safe_load(open(include, "r"))
    config.update(include_config)

config.pop("includes")

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

args.timestamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
if args.identifier:
    args.timestamp += "-{}".format(args.identifier)
args.checkpoint_dir = os.path.join("checkpoints", args.timestamp)
args.results_dir = os.path.join("results", args.timestamp)
args.logs_dir = os.path.join("logs", args.timestamp)

os.makedirs(args.checkpoint_dir)
os.makedirs(args.results_dir)
os.makedirs(args.logs_dir)

print(yaml.dump(config, default_flow_style=False))
for arg in vars(args):
    print("{:<20}: {}".format(arg, getattr(args, arg)))

config["cmd"] = args.__dict__
del args

# Dump config parameters
json.dump(
    config,
    open(os.path.join(config["cmd"]["checkpoint_dir"], "config.json"), "w"),
)

# Tensorboard
log_writer = SummaryWriter(config["cmd"]["logs_dir"])


def main():
    # =========================================================================
    #   SETUP DATALOADER, NORMALIZER, MODEL, LOSS, OPTIMIZER
    # =========================================================================

    best_mae_error = 1e10

    # TODO: move this out to a separate dataloader interface.
    print("### Loading {}".format(config["task"]["dataset"]))
    if config["task"]["dataset"] == "ulissigroup_co":
        dataset = UlissigroupCO(config["dataset"]["src"]).shuffle()
        num_targets = 1
    elif config["task"]["dataset"] == "xie_grossman_mat_proj":
        dataset = XieGrossmanMatProj(config["dataset"]["src"]).shuffle()
        num_targets = 1
    elif config["task"]["dataset"] == "qm9":
        dataset = QM9(config["dataset"]["src"]).shuffle()
        num_targets = dataset.data.y.shape[-1]
        if "label_index" in config["task"]:
            dataset.data.y = dataset.data.y[
                :, int(config["task"]["label_index"])
            ]
            num_targets = 1
    else:
        raise NotImplementedError

    tr_sz, va_sz, te_sz = (
        config["dataset"]["train_size"],
        config["dataset"]["val_size"],
        config["dataset"]["test_size"],
    )

    assert len(dataset) > tr_sz + va_sz + te_sz

    train_dataset = dataset[:tr_sz]
    val_dataset = dataset[tr_sz : tr_sz + va_sz]
    test_dataset = dataset[tr_sz + va_sz : tr_sz + va_sz + te_sz]

    train_loader = DataLoader(
        train_dataset, batch_size=config["optim"]["batch_size"], shuffle=True
    )
    val_loader = DataLoader(
        val_dataset, batch_size=config["optim"]["batch_size"]
    )
    test_loader = DataLoader(
        test_dataset, batch_size=config["optim"]["batch_size"]
    )

    # Compute mean, std of training set labels.
    normalizer = Normalizer(dataset.data.y[:tr_sz], device)

    # Build model
    model = CGCNN(
        dataset.data.x.shape[-1],
        dataset.data.edge_attr.shape[-1],
        num_targets,
        **config["model"],
    ).to(device)

    num_params = sum(p.numel() for p in model.parameters())
    print(
        "### Loaded {} with {} parameters.".format(
            model.__class__.__name__, num_params
        )
    )

    criterion = nn.L1Loss()

    optimizer = optim.AdamW(model.parameters(), config["optim"]["lr_initial"])

    def lr_lambda_fun(current_epoch):
        """Returns a learning rate multiplier.
        Till `warmup_epochs`, learning rate linearly increases to `initial_lr`,
        and then gets multiplied by `lr_gamma` every time a milestone is crossed.
        """
        if current_epoch <= config["optim"]["warmup_epochs"]:
            alpha = current_epoch / float(config["optim"]["warmup_epochs"])
            return config["optim"]["warmup_factor"] * (1.0 - alpha) + alpha
        else:
            idx = bisect(config["optim"]["lr_milestones"], current_epoch)
            return pow(config["optim"]["lr_gamma"], idx)

    scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lr_lambda_fun)

    # =========================================================================
    #   TRAINING LOOP
    # =========================================================================

    for epoch in range(config["optim"]["max_epochs"]):
        # train for one epoch
        train(train_loader, model, criterion, optimizer, epoch, normalizer)

        # evaluate on validation set
        mae_error = validate(val_loader, model, criterion, epoch, normalizer)

        scheduler.step()

        # remember the best mae_eror and save checkpoint
        if config["task"]["type"] == "regression":
            is_best = mae_error < best_mae_error
            best_mae_error = min(mae_error, best_mae_error)
        else:
            is_best = mae_error > best_mae_error
            best_mae_error = max(mae_error, best_mae_error)
        save_checkpoint(
            {
                "epoch": epoch + 1,
                "state_dict": model.state_dict(),
                "best_mae_error": best_mae_error,
                "optimizer": optimizer.state_dict(),
                "normalizer": normalizer.state_dict(),
                "config": config,
            },
            is_best,
            config["cmd"]["checkpoint_dir"],
        )

    # Evaluate best model
    print("---------Evaluate Model on Test Set---------------")
    best_checkpoint = torch.load(
        os.path.join(config["cmd"]["checkpoint_dir"], "model_best.pth.tar")
    )
    model.load_state_dict(best_checkpoint["state_dict"])
    validate(test_loader, model, criterion, epoch, normalizer, test=True)


def train(train_loader, model, criterion, optimizer, epoch, normalizer):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    mae_errors = AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()
    for i, data in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        data = data.to(device)

        # normalize target
        target_normed = normalizer.norm(data.y)

        # compute output
        output = model(data)
        if data.y.dim() == 1:
            output = output.view(-1)
        loss = criterion(output, target_normed)

        # measure accuracy and record loss
        mae_error = eval(config["task"]["metric"])(
            normalizer.denorm(output).cpu(), data.y.cpu()
        )
        losses.update(loss.item(), data.y.size(0))
        mae_errors.update(mae_error, data.y.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        log_writer.add_scalar(
            "Training Loss", losses.val, epoch * len(train_loader) + i
        )
        log_writer.add_scalar(
            "Training MAE", mae_errors.val, epoch * len(train_loader) + i
        )
        log_writer.add_scalar(
            "Learning rate",
            optimizer.param_groups[0]["lr"],
            epoch * len(train_loader) + i,
        )

        if i % config["cmd"]["print_every"] == 0:
            print(
                "Epoch: [{0}][{1}/{2}]\t"
                "Loss: {loss.val:.4f} ({loss.avg:.4f})\t"
                "MAE: {mae_errors.val:.3f} ({mae_errors.avg:.3f})\t"
                "Data: {data_time.val:.3f}s\t"
                "Fwd/bwd: {batch_time.val:.3f}s\t".format(
                    epoch,
                    i,
                    len(train_loader),
                    batch_time=batch_time,
                    data_time=data_time,
                    loss=losses,
                    mae_errors=mae_errors,
                )
            )


def validate(val_loader, model, criterion, epoch, normalizer, test=False):
    batch_time = AverageMeter()
    losses = AverageMeter()
    mae_errors = AverageMeter()

    if test:
        test_targets = []
        test_preds = []
        test_cif_ids = []

    # switch to evaluate mode
    model.eval()

    end = time.time()
    for i, data in enumerate(val_loader):
        data = data.to(device)

        # normalize target
        target_normed = normalizer.norm(data.y)

        # compute output
        output = model(data)
        if data.y.dim() == 1:
            output = output.view(-1)
        loss = criterion(output, target_normed)

        # measure accuracy and record loss
        mae_error = eval(config["task"]["metric"])(
            normalizer.denorm(output).cpu(), data.y.cpu()
        )
        losses.update(loss.item(), data.y.size(0))
        mae_errors.update(mae_error, data.y.size(0))
        if test:
            test_pred = normalizer.denorm(output).cpu()
            test_target = data.y
            test_preds += test_pred.view(-1).tolist()
            test_targets += test_target.view(-1).tolist()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if not test:
            log_writer.add_scalar(
                "Validation Loss", losses.val, epoch * len(val_loader) + i
            )
            log_writer.add_scalar(
                "Validation MAE", mae_errors.val, epoch * len(val_loader) + i
            )

        if i % config["cmd"]["print_every"] == 0:
            print(
                "Val:   [{0}/{1}]\t\t"
                "Loss: {loss.val:.4f} ({loss.avg:.4f})\t"
                "MAE: {mae_errors.val:.3f} ({mae_errors.avg:.3f})\t"
                "Fwd: {batch_time.val:.3f}s\t".format(
                    i,
                    len(val_loader),
                    batch_time=batch_time,
                    loss=losses,
                    mae_errors=mae_errors,
                )
            )

    if config["task"]["dataset"] == "qm9":
        print(
            "MAE",
            torch.mean(
                torch.abs(data.y.cpu() - normalizer.denorm(output).cpu()),
                dim=0,
            ).data.numpy(),
        )

    if test:
        star_label = "**"
        import csv

        with open(
            os.path.join(config["cmd"]["results_dir"], "test_results.csv"), "w"
        ) as f:
            writer = csv.writer(f)
            for cif_id, target, pred in zip(
                test_cif_ids, test_targets, test_preds
            ):
                writer.writerow((cif_id, target, pred))
    else:
        star_label = "*"

    print(
        " {star} MAE {mae_errors.avg:.3f}".format(
            star=star_label, mae_errors=mae_errors
        )
    )
    return mae_errors.avg


if __name__ == "__main__":
    main()
