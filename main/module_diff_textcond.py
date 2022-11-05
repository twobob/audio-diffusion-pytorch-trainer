import random
from typing import Any, Callable, List, Optional

import librosa
import plotly.graph_objs as go
import pytorch_lightning as pl
import torch
import torchaudio
import wandb
from a_transformers_pytorch.transformers import Transformer
from audio_data_pytorch.utils import fractional_random_split
from audio_diffusion_pytorch import AudioDiffusionModel, Sampler, Schedule
from einops import rearrange
from ema_pytorch import EMA
from pytorch_lightning import Callback, Trainer
from pytorch_lightning.loggers import WandbLogger
from torch import Tensor, nn
from torch.utils.data import DataLoader
from transformers import AutoTokenizer

""" Model """


class Model(pl.LightningModule):
    def __init__(
        self,
        lr: float,
        lr_beta1: float,
        lr_beta2: float,
        lr_eps: float,
        lr_weight_decay: float,
        ema_beta: float,
        ema_power: float,
        model: nn.Module,
        tokenizer: str,
        encoder: nn.Module,
        encoder_num_tokens: int,
        encoder_max_length: int,
        encoder_features: int,
    ):
        super().__init__()
        self.lr = lr
        self.lr_beta1 = lr_beta1
        self.lr_beta2 = lr_beta2
        self.lr_eps = lr_eps
        self.lr_weight_decay = lr_weight_decay
        # Diffusion Model
        self.model = model
        self.model_ema = EMA(self.model, beta=ema_beta, power=ema_power)
        # Text Encoder
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer)
        self.to_embedding = nn.Embedding(encoder_num_tokens, encoder_features)
        self.max_length = encoder_max_length
        self.text_encoder = encoder

    @property
    def device(self):
        return next(self.parameters()).device

    def get_text_embeddings(self, texts: List[str]) -> Tensor:
        # Compute batch of tokens and mask from texts
        encoded = self.tokenizer.batch_encode_plus(
            texts,
            return_tensors="pt",
            padding="max_length",
            max_length=self.max_length,
            truncation=True,
        )
        tokens = encoded["input_ids"].to(self.device)
        # mask = encoded["attention_mask"].to(self.device).bool()
        # Compute embedding
        embedding = self.to_embedding(tokens)
        # Encode with transformer
        embedding_encoded = self.text_encoder(embedding)
        return embedding_encoded

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(
            list(self.model.parameters()),
            lr=self.lr,
            betas=(self.lr_beta1, self.lr_beta2),
            eps=self.lr_eps,
            weight_decay=self.lr_weight_decay,
        )
        return optimizer

    def training_step(self, batch, batch_idx):
        waveforms, info = batch
        texts = [random.choice(t["text"]) for t in info]
        embedding = self.get_text_embeddings(texts)
        loss = self.model(waveforms, embedding=embedding)
        self.log("train_loss", loss)
        # Update EMA model and log decay
        self.model_ema.update()
        self.log("ema_decay", self.model_ema.get_current_decay())
        return loss

    def validation_step(self, batch, batch_idx):
        waveforms, info = batch
        texts = [random.choice(t["text"]) for t in info]
        embedding = self.get_text_embeddings(texts)
        loss = self.model_ema(waveforms, embedding=embedding)
        self.log("valid_loss", loss)
        return loss


""" Datamodule """


class Datamodule(pl.LightningDataModule):
    def __init__(
        self,
        dataset_train,
        dataset_valid,
        *,
        num_workers: int,
        pin_memory: bool = False,
        **kwargs: int,
    ) -> None:
        super().__init__()
        self.dataset_train = dataset_train
        self.dataset_valid = dataset_valid
        self.num_workers = num_workers
        self.pin_memory = pin_memory
        self.data_train: Any = None
        self.data_val: Any = None

    def train_dataloader(self) -> DataLoader:
        return DataLoader(
            dataset=self.dataset_train,
            batch_size=None,
            num_workers=self.num_workers,
            pin_memory=self.pin_memory,
        )

    def val_dataloader(self) -> DataLoader:
        return DataLoader(
            dataset=self.dataset_valid,
            batch_size=None,
            num_workers=self.num_workers,
            pin_memory=self.pin_memory,
        )


""" Callbacks """


def get_wandb_logger(trainer: Trainer) -> Optional[WandbLogger]:
    """Safely get Weights&Biases logger from Trainer."""

    if isinstance(trainer.logger, WandbLogger):
        return trainer.logger

    else: 
        for logger in trainer.loggers:
            if isinstance(logger, WandbLogger):
                return logger

    print("WandbLogger not found.")
    return None


def log_wandb_audio_batch(
    logger: WandbLogger, id: str, samples: Tensor, sampling_rate: int, caption: str = ""
):
    num_items = samples.shape[0]
    samples = rearrange(samples, "b c t -> b t c").detach().cpu().numpy()
    logger.log(
        {
            f"sample_{idx}_{id}": wandb.Audio(
                samples[idx],
                caption=caption,
                sample_rate=sampling_rate,
            )
            for idx in range(num_items)
        }
    )


def log_wandb_audio_spectrogram(
    logger: WandbLogger, id: str, samples: Tensor, sampling_rate: int, caption: str = ""
):
    num_items = samples.shape[0]
    samples = samples.detach().cpu()
    transform = torchaudio.transforms.MelSpectrogram(
        sample_rate=sampling_rate,
        n_fft=1024,
        hop_length=512,
        n_mels=80,
        center=True,
        norm="slaney",
    )

    def get_spectrogram_image(x):
        spectrogram = transform(x[0])
        image = librosa.power_to_db(spectrogram)
        trace = [go.Heatmap(z=image, colorscale="viridis")]
        layout = go.Layout(
            yaxis=dict(title="Mel Bin (Log Frequency)"),
            xaxis=dict(title="Frame"),
            title_text=caption,
            title_font_size=10,
        )
        fig = go.Figure(data=trace, layout=layout)
        return fig

    logger.log(
        {
            f"mel_spectrogram_{idx}_{id}": get_spectrogram_image(samples[idx])
            for idx in range(num_items)
        }
    )


class SampleLogger(Callback):
    def __init__(
        self,
        num_items: int,
        channels: int,
        sampling_rate: int,
        length: int,
        sampling_steps: List[int],
        embedding_scale: int,
        diffusion_schedule: Schedule,
        diffusion_sampler: Sampler,
        use_ema_model: bool,
    ) -> None:
        self.num_items = num_items
        self.channels = channels
        self.sampling_rate = sampling_rate
        self.length = length
        self.sampling_steps = sampling_steps
        self.embedding_scale = embedding_scale
        self.diffusion_schedule = diffusion_schedule
        self.diffusion_sampler = diffusion_sampler
        self.use_ema_model = use_ema_model

        self.log_next = False

    def on_validation_epoch_start(self, trainer, pl_module):
        self.log_next = True

    def on_validation_batch_start(
        self, trainer, pl_module, batch, batch_idx, dataloader_idx
    ):
        if self.log_next:
            self.log_sample(trainer, pl_module, batch)
            self.log_next = False

    @torch.no_grad()
    def log_sample(self, trainer, pl_module, batch):
        is_train = pl_module.training
        if is_train:
            pl_module.eval()

        wandb_logger = get_wandb_logger(trainer).experiment
        diffusion_model = pl_module.model

        if self.use_ema_model:
            diffusion_model = pl_module.model_ema.ema_model

        waveform, info = batch
        waveform = waveform[0 : self.num_items]

        log_wandb_audio_batch(
            logger=wandb_logger,
            id="true",
            samples=waveform,
            sampling_rate=self.sampling_rate,
        )
        log_wandb_audio_spectrogram(
            logger=wandb_logger,
            id="true",
            samples=waveform,
            sampling_rate=self.sampling_rate,
        )

        text = [random.choice(t["text"]) for t in info[0 : self.num_items]]
        embedding = pl_module.get_text_embeddings(text)

        noise = torch.randn(
            (self.num_items, self.channels, self.length), device=pl_module.device
        )

        for steps in self.sampling_steps:
            samples = diffusion_model.sample(
                noise=noise,
                embedding=embedding,
                embedding_scale=self.embedding_scale,
                sampler=self.diffusion_sampler,
                sigma_schedule=self.diffusion_schedule,
                num_steps=steps,
            )
            log_wandb_audio_batch(
                logger=wandb_logger,
                id="sample",
                samples=samples,
                sampling_rate=self.sampling_rate,
                caption=f"Sampled in {steps} steps",
            )
            log_wandb_audio_spectrogram(
                logger=wandb_logger,
                id="sample",
                samples=samples,
                sampling_rate=self.sampling_rate,
                caption=f"Sampled in {steps} steps",
            )

        if is_train:
            pl_module.train()
