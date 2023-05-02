r"""Train Image-and-Spatial Transformer Network."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime
import logging
import math
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Union

import typer
from typer import Option

from ignite import distributed as idist
from ignite.contrib.handlers.param_scheduler import LRScheduler
from ignite.engine import DeterministicEngine, Engine, Events
from ignite.metrics import Average, BatchWise, EpochWise, Metric, MetricUsage
from ignite.handlers import Checkpoint, DiskSaver, global_step_from_engine
from ignite.utils import manual_seed, setup_logger

import torch
import torch.backends.cudnn
from torch import Tensor
from torch.nn import Module, Sequential, functional as F
from torch.optim import Adam, Optimizer
from torch.optim.lr_scheduler import ExponentialLR
from torch.utils.data import DataLoader, Dataset, Subset
from torch.utils.data.distributed import DistributedSampler
from torch.utils.tensorboard import SummaryWriter

from deepali.core import DataclassConfig, functional as U, unlink_or_mkdir
from deepali.data import ImageBatch, ImageDataset, ImageDatasetConfig, Partition
from deepali.data import collate_samples, prepare_batch
from deepali.data.transforms import CastImage, ResizeImage
from deepali.modules import ToImmutableOutput
from deepali.utils.cli import LOG_FORMAT, LogLevel, cuda_visible_devices
from deepali.utils.cli import filter_warning_of_experimental_named_tensors_feature
from deepali.utils.ignite import handlers as H
from deepali.utils.ignite.output_transforms import get_output_transform
from deepali.utils.ignite.score_functions import negative_loss_score_function

from models import ImageAndSpatialTransformerConfig as ModelConfig
from models import ImageAndSpatialTransformerNetwork, create_istn


LossFunction = Callable[[Dict[str, Tensor]], Dict[str, Tensor]]


@dataclass
class DatasetsConfig(DataclassConfig):
    r"""Training dataset configuraiton."""

    train: ImageDatasetConfig
    valid: ImageDatasetConfig

    @classmethod
    def _from_dict(cls, arg: Mapping[str, Any], parent: Optional[Path] = None) -> Config:
        r"""Create configuration from dictionary."""
        if "train" not in arg:
            raise ValueError(
                f"{cls.__name__}.from_dict() 'arg' must contain 'train' dataset configuration"
            )
        if "valid" not in arg:
            raise ValueError(
                f"{cls.__name__}.from_dict() 'arg' must contain 'valid' dataset configuration"
            )
        train = ImageDatasetConfig.from_dict(arg["train"], parent=parent)
        valid = ImageDatasetConfig.from_dict(arg["valid"], parent=parent)
        return cls(train=train, valid=valid)


@dataclass
class TrainConfig(DataclassConfig):
    r"""Training hyperparameters."""

    # Initialization
    random_seed: int = 0
    deterministic: bool = True
    # Data loading
    batch_size: int = 10
    num_samples: int = -1
    num_workers_per_proc: int = 2
    num_workers_per_node: int = 10
    pin_memory: bool = True
    shuffle: bool = True
    # Loss function
    loss: str = "explicit"
    # Optimization parameters
    max_epochs: int = 100
    max_iterations: int = 0
    max_learning_rate: float = 0.01
    min_learning_rate: float = 0.0001
    learning_rate_decay_steps: int = 0
    learning_rate_decay_rate: float = 0.9
    # Logging settings
    log_every: int = 1
    checkpoint_every: int = 10
    checkpoint_lastn: int = 10
    checkpoint_score: str = "neg_loss"
    summary_every: int = 1
    summary_batches: bool = False
    summary_hists: bool = False
    summary_images: bool = True
    summary_graph: bool = False
    summary_grid: bool = True
    summary_grid_inverted: bool = True
    summary_grid_stride: Union[int, Sequence[int]] = (5, 5)
    # Evaluation on validation set
    eval_batch_size: int = -1
    eval_every_steps: int = 10
    eval_save_firstn: int = 5
    eval_num_samples: int = -1
    eval_num_workers: int = -1


@dataclass
class Config(DataclassConfig):
    r"""Dataset, model, and training configuration."""

    dataset: DatasetsConfig
    model: ModelConfig
    train: TrainConfig

    @classmethod
    def _from_dict(cls, arg: Mapping[str, Any], parent: Optional[Path] = None) -> Config:
        r"""Create configuration from dictionary."""
        if "dataset" not in arg:
            raise ValueError(
                f"{cls.__name__}.from_dict() 'arg' must contain 'dataset' configuration"
            )
        dataset = DatasetsConfig.from_dict(arg["dataset"], parent=parent)
        model = ModelConfig.from_dict(arg.get("model", {}), parent=parent)
        train = TrainConfig.from_dict(arg.get("train", {}), parent=parent)
        return cls(dataset=dataset, model=model, train=train)


def main(
    config_path: Path = Option(
        "params.yaml", "--config", "-c", exists=True, help="Configuration YAML file."
    ),
    log_dir: Path = Option("log/train", help="TensorBoard log directory."),
    log_level: LogLevel = Option(LogLevel.INFO, case_sensitive=False, help="Logging level name."),
    detect_anomaly: bool = Option(False, help="Enable anomaly detection."),
) -> None:
    r"""Invoke data parallel training."""

    now_dt = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    logger = setup_logger("train", level=int(log_level), format=LOG_FORMAT, distributed_rank=0)

    # IDs of GPUs on which to execute training
    gpu_ids = cuda_visible_devices()
    if torch.cuda.device_count() != len(gpu_ids):
        logger.error(
            "CUDA device count (%d) must match environment variable CUDA_VISIBLE_DEVICES (%d)",
            torch.cuda.device_count(),
            len(gpu_ids),
        )
        return 1

    # Load configuration
    config = Config.read(config_path)

    # Save configuration to output log directory
    log_dir = str(log_dir).format(now=now_dt)
    log_dir: Path = Path(log_dir).absolute()
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.debug(f"Write '{log_dir / 'train.yaml'}'")
    config.write(log_dir / "train.yaml")

    # Spawn processes for parallel model training
    spawn_kwargs = {}
    if len(gpu_ids) > 1:
        spawn_kwargs["backend"] = "nccl"
        spawn_kwargs["nproc_per_node"] = len(gpu_ids)
    with idist.Parallel(**spawn_kwargs) as parallel:
        parallel.run(
            train_local,
            config=config,
            log_dir=log_dir,
            log_level=log_level,
            now_dt=now_dt,
            detect_anomaly=detect_anomaly,
        )
    return 0


def train_local(
    local_rank: int,
    config: Config,
    log_dir: Path,
    log_level: Union[int, LogLevel],
    now_dt: str,
    trained_model_path: Optional[str] = None,
    detect_anomaly: bool = False,
) -> None:
    r"""Run model training in local distributed process."""
    filter_warning_of_experimental_named_tensors_feature()

    rank = idist.get_rank()
    seed = config.train.random_seed + rank
    logger = setup_logger("train", level=int(log_level), format=LOG_FORMAT, distributed_rank=rank)

    logger.info(
        f"distributed backend={idist.backend()}, seed={seed}"
        f", rank={rank}, node_rank={idist.get_node_rank()}, local_rank={local_rank}"
    )
    logging.getLogger("ignite").setLevel(logging.WARNING)

    # Seed all random number generators
    manual_seed(seed)

    # Configure PyTorch
    torch.backends.cudnn.benchmark = False if config.train.deterministic else True
    torch.backends.cudnn.deterministic = config.train.deterministic
    torch.autograd.set_detect_anomaly(detect_anomaly)

    # Train ISTN
    if rank == 0 and log_dir:
        writer_cm = contextlib.closing(SummaryWriter(str(log_dir)))
    else:
        writer_cm = contextlib.nullcontext()
    with writer_cm as writer:
        istn = train(config, log_dir=log_dir, log_level=log_level, logger=logger, writer=writer)

    # Save best trained model
    if rank == 0 and trained_model_path:
        path = trained_model_path.format(logdir=log_dir.as_posix(), now=now_dt)
        path = unlink_or_mkdir(path)
        torch.save(istn.state_dict(), path)


def train(
    config: Config,
    load_model_path: Optional[Path] = None,
    checkpoint_path: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    log_level: Union[int, LogLevel] = LogLevel.INFO,
    logger: Optional[logging.Logger] = None,
    writer: Optional[SummaryWriter] = None,
) -> ImageAndSpatialTransformerNetwork:
    r"""Train Image-and-Spatial Transformer Network."""

    rank = idist.get_rank()
    device = idist.device()

    if logger is None:
        logger = setup_logger(
            "train", level=int(log_level), format=LOG_FORMAT, distributed_rank=rank
        )

    # Initialize data loaders
    train_set = create_dataset(config, Partition.TRAIN)
    valid_set = create_dataset(config, Partition.VALID)

    if config.train.num_samples and config.train.num_samples > 0:
        train_set = Subset(train_set, list(range(config.train.num_samples)))
    if config.train.eval_num_samples and config.train.eval_num_samples > 0:
        valid_set = Subset(valid_set, list(range(config.train.eval_num_samples)))

    train_batches = create_dataloader(config, Partition.TRAIN, train_set)
    valid_batches = create_dataloader(config, Partition.VALID, valid_set)

    # Create and log model
    istn = create_istn(config.model)
    istn = istn.to(device=device)
    if rank == 0 and log_dir:
        model_txt = unlink_or_mkdir(log_dir / "model.txt")
        with model_txt.open("wt") as fp:
            print(istn, file=fp)
        if config.train.summary_graph and writer is not None:
            batch = next(iter(valid_batches))
            writer.add_graph(Sequential(istn, ToImmutableOutput()), [batch])
            del batch

    # Initialize parameters from pretrained model
    # before wrapping it in DistributedDataParallel
    is_pretrained = False
    if not checkpoint_path and load_model_path:
        if rank == 0:
            logger.info(f"Loading parameters from '{load_model_path}'")
        state_dict = torch.load(load_model_path, map_location=device)
        if "model" in state_dict:
            state_dict = state_dict["model"]
        if "state" in state_dict:
            state_dict = state_dict["state"]
        istn.load_state_dict(state_dict)
        is_pretrained = True

    # Wrap model in (Distributed)DataParallel
    model = idist.auto_model(istn)

    # Loss function
    loss = create_loss(config.train)

    # Setup optimizer
    optimizer = create_optimizer(config.train, model)
    optimizer = idist.auto_optim(optimizer)

    # Create training engine
    trainer: Engine = create_trainer(
        config.train, model, loss, optimizer, device=device, non_blocking=True
    )
    if isinstance(train_batches.sampler, DistributedSampler):
        trainer.add_event_handler(Events.EPOCH_STARTED, H.set_distributed_sampler_epoch)
    if config.train.log_every > 0:
        log_event = Events.ITERATION_COMPLETED(every=config.train.log_every)
        if config.train.log_every > 1:
            log_event |= Events.ITERATION_COMPLETED(once=1)
        trainer.add_event_handler(log_event, H.print_metrics, logger=logger)

    # Resume training from previous state and add handler to save checkpoints
    chkpt_objs = {"trainer": trainer, "model": model, "optimizer": optimizer}
    if checkpoint_path:
        if rank == 0:
            logger.info(f"Loading checkpoint '{checkpoint_path}'")
        chkpt = torch.load(checkpoint_path, map_location=device)
        Checkpoint.load_objects(to_load=chkpt_objs, checkpoint=chkpt)
    if config.train.checkpoint_every > 0:
        trainer.add_event_handler(
            Events.ITERATION_COMPLETED(every=config.train.checkpoint_every),
            checkpoint_writer(config.train, log_dir, chkpt_objs),
        )

    # Initial best state
    best_state_dict = model.state_dict()
    best_loss_value = float("inf")

    # Add handlers to save training summary to tensorboard event file
    if writer is not None and config.train.summary_every > 0:
        save_event = Events.ITERATION_COMPLETED(every=config.train.summary_every)
        if config.train.summary_every > 1:
            save_event |= Events.ITERATION_COMPLETED(once=1)
        trainer.add_event_handler(save_event, H.write_summary_metrics, writer, prefix="train/")
        if config.train.summary_batches:
            trainer.add_event_handler(
                save_event,
                H.write_summary_images,
                writer,
                prefix="train/",
                names={
                    "source_img",
                    "target_img",
                    "warped_img",
                    "source_seg",
                    "target_seg",
                    "warped_seg",
                    "source_soi",
                    "target_soi",
                    "warped_soi",
                    "warped_grid",
                },
                rescale_transform=normalize_summary_image,
            )
        if config.train.summary_hists:
            trainer.add_event_handler(save_event, H.write_summary_hists, writer, model)
        trainer.add_event_handler(
            save_event,
            H.write_summary_optim_params,
            writer,
            optimizer,
            prefix="optim/",
        )

    # Setup model evaluator
    eval_epoch_length = len(valid_batches)
    eval_every_steps = config.train.eval_every_steps
    if eval_every_steps < 0:
        eval_every_steps = config.train.summary_every
    if len(valid_batches) > 0 and eval_every_steps > 0:
        global_step_fn = global_step_from_engine(trainer)
        evaluator = create_evaluator(config.train, model, loss, device=device, non_blocking=True)
        if writer is not None:
            evaluator.add_event_handler(
                Events.COMPLETED,
                H.write_summary_metrics,
                writer,
                prefix="valid/",
                global_step_transform=global_step_fn,
            )
            if config.train.summary_images and config.train.eval_save_firstn != 0:

                def write_output_event_filter(_: Engine, event: int) -> bool:
                    if config.train.eval_save_firstn < 0:
                        return True
                    return event <= config.train.eval_save_firstn

                def write_input_event_filter(engine: Engine, event: int) -> bool:
                    if trainer.state.iteration > eval_every_steps:
                        return False
                    return write_output_event_filter(engine, event)

                prefix = "valid/{i}/"
                evaluator.add_event_handler(
                    Events.ITERATION_COMPLETED(write_input_event_filter),
                    H.write_summary_images,
                    writer,
                    prefix=prefix,
                    names={
                        "source_img",
                        "target_img",
                        "source_seg",
                        "target_seg",
                    },
                    rescale_transform=normalize_summary_image,
                    global_step_transform=global_step_fn,
                )
                evaluator.add_event_handler(
                    Events.ITERATION_COMPLETED(write_output_event_filter),
                    H.write_summary_images,
                    writer,
                    prefix=prefix,
                    names={
                        "source_soi",
                        "target_soi",
                        "warped_img",
                        "warped_seg",
                        "warped_soi",
                        "warped_grid",
                    },
                    rescale_transform=normalize_summary_image,
                    global_step_transform=global_step_fn,
                )
                del prefix

        eval_logger = setup_logger(
            "valid", level=int(log_level), format=LOG_FORMAT, distributed_rank=rank
        )
        evaluator.add_event_handler(
            Events.COMPLETED, H.print_metrics, logger=eval_logger, prefix=""
        )

        def evaluate(_: Engine):
            eval_logger.info(f"Evaluate metrics on validation set of size N={len(valid_set)}.")
            evaluator.run(valid_batches, max_epochs=1, epoch_length=eval_epoch_length)
            mean_loss = float(evaluator.state.metrics["loss"])
            nonlocal best_loss_value
            if mean_loss < best_loss_value:
                best_loss_value = mean_loss
                eval_logger.debug(
                    f"Copy state dict of new best model with average loss={mean_loss}."
                )
                nonlocal best_state_dict
                for name, value in model.state_dict().items():
                    best_state_dict[name].copy_(value)

        eval_event = Events.ITERATION_COMPLETED(every=eval_every_steps)
        if is_pretrained:
            eval_event |= Events.STARTED
        trainer.add_event_handler(eval_event, evaluate)

    # Run model training
    epoch_length = len(train_batches)
    max_epochs = config.train.max_epochs
    if config.train.max_iterations > 0:
        max_epochs = min(max_epochs, int(math.ceil(config.train.max_iterations / epoch_length)))
    trainer.run(train_batches, max_epochs=max_epochs, epoch_length=epoch_length)

    # Return trained model
    istn.load_state_dict(best_state_dict)
    return istn


def process_function(
    config: TrainConfig,
    istn: ImageAndSpatialTransformerNetwork,
    loss_fn: LossFunction,
    optimizer: Optional[Optimizer] = None,
    device: Optional[torch.device] = None,
    non_blocking: bool = False,
) -> Callable[[Engine, Dict[str, Union[str, ImageBatch, None]]], Dict[str, Tensor]]:
    r"""Process function of Ignite training or evaluation engine."""

    def detached(output: Dict[str, Tensor]) -> Dict[str, Tensor]:
        return {name: tensor.detach() for name, tensor in output.items()}

    def process_batch(_: Engine, batch: Dict[str, ImageBatch]) -> Dict[str, Tensor]:
        output: Dict[str, Tensor] = {}
        istn.train(optimizer is not None)
        with torch.set_grad_enabled(istn.training):
            # Copy input to device
            batch = prepare_batch(batch, device=device, non_blocking=non_blocking)
            output.update({k: v.tensor() for k, v in batch.items() if k != "meta"})
            # Evaluate ISTN output
            source_img = output["source_img"]
            target_img = output["target_img"]
            output.update(istn(source_img, target_img, apply=False))
            source_images = {k: v for k, v in output.items() if k.startswith("source_")}
            warped_images: Dict[str, Tensor] = istn.warp(source_images)
            output.update({k.replace("source_", "warped_"): v for k, v in warped_images.items()})
            # Render warped grid lines for visualization in TensorBoard
            if config.summary_grid:
                grid_image = U.grid_image(
                    shape=source_img.shape[2:],
                    stride=config.summary_grid_stride,
                    inverted=config.summary_grid_inverted,
                    device=device,
                )
                output["warped_grid"] = istn.warp(grid_image)
            # Evaluate loss terms
            losses = loss_fn(output)
            output.update(losses)
            if optimizer is not None:
                optimizer.zero_grad()
                losses["loss"].backward()
                optimizer.step()
        return detached(output)

    return process_batch


def attach_metrics(
    engine: Engine,
    usage: Union[MetricUsage, str] = EpochWise(),
) -> Engine:
    r"""Attach evaluation metrics to given Ignite engine."""

    loss = Average(get_output_transform("loss"))

    metrics: Dict[str, Metric] = {"loss": loss}
    # TODO: Additional metrics

    for name, metric in metrics.items():
        metric.attach(engine, name, usage=usage)
    return engine


def get_batch_size(config: TrainConfig, partition: Partition) -> int:
    batch_size = config.batch_size
    if partition is Partition.VALID and config.eval_batch_size > 0:
        batch_size = config.eval_batch_size
    return batch_size


def create_dataset(config: Config, partition: Union[Partition, str]) -> ImageDataset:
    partition = Partition(partition)
    if partition is Partition.TRAIN:
        dataset = ImageDataset.from_config(config.dataset.train)
    elif partition is Partition.VALID:
        dataset = ImageDataset.from_config(config.dataset.valid)
    else:
        raise ValueError(
            f"Dataset partition must be {Partition.TRAIN.value!r} or {Partition.VALID.value!r}"
        )
    keys = config.dataset.train.images.keys()
    transform = dataset.transform()
    transform.add_module("cast", CastImage.item(keys, dtype=torch.float32))
    transform.add_module("resize", ResizeImage.item(keys, size=config.model.input.size))
    return dataset


def create_dataloader(
    config: Config, partition: Union[Partition, str], dataset: Optional[Dataset] = None
) -> DataLoader:
    partition = Partition(partition)
    if dataset is None:
        dataset = create_dataset(config, partition)
    world_size = idist.get_world_size()
    shuffle = partition is Partition.TRAIN and config.train.shuffle
    if partition is Partition.TRAIN:
        num_workers = config.train.num_workers_per_proc
    else:
        num_workers = config.train.eval_num_workers
        if num_workers is None or num_workers < 0:
            num_workers = config.train.num_workers_per_proc
    batch_size = get_batch_size(config.train, partition)
    num_workers = max(0, min(num_workers * world_size, config.train.num_workers_per_node))
    persistent_workers = num_workers > 0 and partition is not Partition.TRAIN
    return idist.auto_dataloader(
        dataset,
        batch_size=batch_size * world_size,
        shuffle=shuffle,
        drop_last=False,
        pin_memory=config.train.pin_memory,
        num_workers=num_workers,
        persistent_workers=persistent_workers,
        collate_fn=collate_samples,
    )


def create_loss(config: TrainConfig) -> LossFunction:
    r"""Get loss function."""

    def loss_fn(output: Dict[str, Tensor]) -> Dict[str, Tensor]:
        r"""Evaluate loss terms given process function output."""
        # Loss terms used for progress monitoring
        loss_itn = F.mse_loss(output["source_soi"], output["source_seg"])
        loss_itn += F.mse_loss(output["target_soi"], output["target_seg"])
        loss_stn_u = F.mse_loss(output["warped_img"], output["target_img"])
        loss_stn_s = F.mse_loss(output["warped_seg"], output["target_seg"])
        loss_stn_i = F.mse_loss(output["warped_soi"], output["target_seg"])
        loss_stn_i += F.mse_loss(output["warped_seg"], output["target_soi"])
        loss_stn_r = F.mse_loss(output["warped_soi"], output["target_soi"])
        # Loss term used for training
        if config.loss in ("e", "explicit"):
            loss_train = loss_itn + loss_stn_s
        elif config.loss in ("i", "implicit"):
            loss_train = loss_stn_i + loss_stn_s
        elif config.loss in ("s", "supervised"):
            loss_train = loss_stn_s
        elif config.loss in ("u", "unsupervised"):
            loss_train = loss_stn_u
        else:
            raise ValueError(f"Invalid loss function: {config.loss}")
        # Return loss terms
        return {
            "loss": loss_train,
            "loss_itn": loss_itn,
            "loss_stn_u": loss_stn_u,
            "loss_stn_s": loss_stn_s,
            "loss_stn_i": loss_stn_i,
            "loss_stn_r": loss_stn_r,
        }

    return loss_fn


def create_optimizer(config: TrainConfig, model: Module) -> Optimizer:
    return Adam(model.parameters(), lr=config.max_learning_rate)


def create_trainer(
    config: TrainConfig,
    model: ImageAndSpatialTransformerNetwork,
    loss_fn: LossFunction,
    optimizer: Optimizer,
    device: Optional[Union[torch.device, str]] = None,
    non_blocking: bool = False,
) -> Engine:
    r"""Create Ignite engine for model training."""
    process_fn = process_function(
        config, model, loss_fn, optimizer=optimizer, device=device, non_blocking=non_blocking
    )
    engine_type = DeterministicEngine if config.deterministic else Engine
    trainer = engine_type(process_fn)
    if config.max_iterations and config.max_iterations > 0:
        trainer.add_event_handler(
            Events.ITERATION_COMPLETED,
            H.terminate_on_max_iteration,
            config.max_iterations,
        )
    if config.learning_rate_decay_steps > 0:
        if 0 < config.learning_rate_decay_rate < 1:
            lr_gamma = config.learning_rate_decay_rate ** (1 / config.learning_rate_decay_steps)
            lr_scheduler = LRScheduler(ExponentialLR(optimizer, lr_gamma))
            trainer.add_event_handler(Events.ITERATION_COMPLETED, lr_scheduler)
        else:
            raise ValueError(
                "create_trainer() 'config' learning rate decay rate must be in open interval (0, 1)."
                " To disable learning rate decay, set learning_rate_decay_steps=0."
            )
        if config.min_learning_rate > 0:
            trainer.add_event_handler(
                Events.ITERATION_STARTED,
                H.clamp_learning_rate,
                optimizer,
                min_learning_rate=config.min_learning_rate,
                max_learning_rate=config.max_learning_rate,
            )
    return attach_metrics(trainer, usage=BatchWise())


def create_evaluator(
    config: TrainConfig,
    model: ImageAndSpatialTransformerNetwork,
    loss_fn: LossFunction,
    device: Optional[torch.device] = None,
    non_blocking: bool = False,
) -> Engine:
    r"""Create Ignite engine for model evaluation."""
    process_fn = process_function(config, model, loss_fn, device=device, non_blocking=non_blocking)
    engine_type = DeterministicEngine if config.deterministic else Engine
    evaluator = engine_type(process_fn)
    return attach_metrics(evaluator, usage=EpochWise())


def checkpoint_writer(
    config: TrainConfig,
    log_dir: Optional[str],
    to_save: Mapping[str, Any],
    filename_pattern: Optional[str] = None,
    global_step_transform: Optional[Callable] = None,
) -> Checkpoint:
    r"""Create checkpoint handler."""
    n_saved = config.checkpoint_lastn
    if n_saved == 0 or not log_dir:
        return
    if n_saved is not None and n_saved < 0:
        n_saved = None
    score_function = None
    score_name = config.checkpoint_score
    if score_name == "neg_loss":
        score_function = negative_loss_score_function
    elif score_name:
        raise ValueError(f"checkpoint_writer() invalid 'config.checkpoint_score': {score_name}")
    if not filename_pattern:
        filename_pattern = "checkpoint"
        if score_name:
            filename_pattern += "_{score_name}={score}"
        else:
            filename_pattern += "_{global_step:06d}"
        filename_pattern += ".{ext}"
    disk_saver = DiskSaver(
        log_dir,
        atomic=True,
        create_dir=True,
        require_empty=False,
    )
    checkpoint_writer = Checkpoint(
        to_save=to_save,
        save_handler=disk_saver,
        score_function=score_function,
        score_name=score_name,
        n_saved=n_saved,
        global_step_transform=global_step_transform,
        filename_pattern=filename_pattern,
    )
    return checkpoint_writer


def normalize_summary_image(tag: str, data: Tensor) -> Tensor:
    r"""Linearly rescale image values to [0, 1] for logging to TensorBoard."""
    if tag.endswith("_seg"):
        return data
    return U.normalize_image(data, mode="unit")


if __name__ == "__main__":
    typer.run(main)
