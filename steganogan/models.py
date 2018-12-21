# -*- coding: utf-8 -*-
import gc
import inspect
import json
import os
import pickle
from collections import Counter
from uuid import uuid4

import imageio
import torch
from imageio import imread, imwrite
from torch.nn.functional import binary_cross_entropy_with_logits, mse_loss
from torch.optim import Adam
from tqdm import tqdm

from steganogan.utils import bits_to_bytearray, bytearray_to_text, ssim, text_to_bits

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'train')

METRIC_FIELDS = [
    'val.encoder_mse',
    'val.decoder_loss',
    'val.decoder_acc',
    'val.cover_score',
    'val.generated_score',
    'val.ssim',
    'val.psnr',
    'val.bpp',
    'train.encoder_mse',
    'train.decoder_loss',
    'train.decoder_acc',
    'train.cover_score',
    'train.generated_score',
]


class SteganoGAN(object):

    def _get_instance(self, class_or_instance, kwargs):
        """Returns an instance of the class"""

        if not inspect.isclass(class_or_instance):
            return class_or_instance

        argspec = inspect.getfullargspec(class_or_instance.__init__).args
        argspec.remove('self')
        init_args = {arg: kwargs[arg] for arg in argspec}

        return class_or_instance(**init_args)

    def _get_device(self):
        """Returns torch device"""
        if self.cuda and torch.cuda.is_available():
            return torch.device('cuda')

        return torch.device('cpu')

    def __init__(self, data_depth, encoder, decoder, critic,
                 cuda=False, train_path=DEFAULT_PATH, fit_log=False, **kwargs):

        self.data_depth = data_depth
        kwargs['data_depth'] = data_depth
        self.encoder = self._get_instance(encoder, kwargs)
        self.decoder = self._get_instance(decoder, kwargs)
        self.critic = self._get_instance(critic, kwargs)
        self.cuda = cuda
        self.device = self._get_device()

        self.critic_optimizer = None
        self.decoder_optimizer = None

        # Misc
        self.train_path = train_path
        self.fit_log = fit_log
        self.train_metrics = None

    def encode(self, image, output, text):
        """Encode an image.
        Args:
            image(str): Path to the image to be used as cover.
            output(str): Path where the generated image will be saved.
            text(str): Message to hide inside the image.
        """
        # Force to use cpu when encode / decode
        if self.cuda:
            self.encoder.to(torch.device('cpu'))

        image = imread(image, pilmode='RGB') / 127.5 - 1.0
        image = torch.FloatTensor(image).permute(2, 1, 0).unsqueeze(0)

        _, _, height, width = image.size()
        payload = self._make_payload(width, height, self.data_depth, text)

        generated_image = self.encoder(image, payload)[0].clamp(-1.0, 1.0)
        #  print(generated_image.min(), generated_image.max())
        generated_image = (generated_image.permute(2, 1, 0).detach().cpu().numpy() + 1.0) * 127.5
        imwrite(output, generated_image.astype('uint8'))
        print('Encoding completed.')

    def decode(self, image):

        if self.cuda:
            self.decoder.to(torch.device('cpu'))

        if not os.path.exists(image):
            raise ValueError('Unable to read %s.' % image)

        # extract a bit vector
        image = imread(image, pilmode='RGB') / 255.0
        image = torch.FloatTensor(image).permute(2, 1, 0).unsqueeze(0)
        image = self.decoder(image).view(-1) > 0

        # split and decode messages
        candidates = Counter()
        bits = image.data.cpu().numpy().tolist()
        for candidate in bits_to_bytearray(bits).split(b'\x00\x00\x00\x00'):
            candidate = bytearray_to_text(bytearray(candidate))
            if candidate:
                candidates[candidate] += 1

        # choose most common message
        if len(candidates) == 0:
            raise ValueError('Failed to find message.')

        candidate, count = candidates.most_common(1)[0]
        return candidate

    def _make_payload(self, width, height, depth, text):
        """
        This takes a piece of text and encodes it into a bit vector. It then
        fills a matrix of size (width, height) with copies of the bit vector.
        """
        message = text_to_bits(text) + [0] * 32

        payload = message
        while len(payload) < width * height * depth:
            payload += message

        payload = payload[:width * height * depth]

        return torch.FloatTensor(payload).view(1, depth, height, width)

    def _random_data(self, cover):
        """Generate random data ready to be hidden inside the cover image.

        Args:
            cover (image): Image to use as cover.

        Returns:
            generated (image): Image generated with the encoded message.
        """
        N, _, H, W = cover.size()
        cover.to(self.device)
        return torch.zeros((N, self.data_depth, H, W), device=self.device).random_(0, 2)

    def _encode_decode(self, cover, quantize=False):
        """Encode random data and then decode it.

        Args:
            cover (image): Image to use as cover.
            quantize (bool): whether to quantize the generated image or not.

        Returns:
            generated (image): Image generated with the encoded message.
            payload (bytes): Random data that has been encoded in the image.
            decoded (bytes): Data decoded from the generated image.
        """
        payload = self._random_data(cover)
        generated = self.encoder(cover, payload)
        if quantize:
            generated = (255.0 * (generated + 1.0) / 2.0).long()
            generated = 2.0 * generated.float() / 255.0 - 1.0

        decoded = self.decoder(generated)

        return generated, payload, decoded

    def _critic(self, image):
        """Evaluate the image using the critic"""
        return torch.mean(self.critic(image))

    def _get_optimizers(self):
        _dec_list = list(self.decoder.parameters()) + list(self.encoder.parameters())
        critic_optimizer = Adam(self.critic.parameters(), lr=1e-4)
        decoder_optimizer = Adam(_dec_list, lr=1e-4)

        return critic_optimizer, decoder_optimizer

    def _create_folders(self):
        # Logging
        os.makedirs(self.train_path, exist_ok=True)
        os.makedirs(self.train_path + '/weights', exist_ok=True)
        os.makedirs(self.train_path + '/samples', exist_ok=True)

    def _fit_critic(self, train, metrics):
        """Critic process"""
        for cover, _ in tqdm(train, disable=not self.fit_log):
            gc.collect()
            payload = self._random_data(cover)
            generated = self.encoder(cover, payload)
            cover_score = self._critic(cover)
            generated_score = self._critic(generated)

            self.critic_optimizer.zero_grad()
            (cover_score - generated_score).backward(retain_graph=True)
            self.critic_optimizer.step()

            for p in self.critic.parameters():
                p.data.clamp_(-0.1, 0.1)

            metrics['train.cover_score'].append(cover_score.item())
            metrics['train.generated_score'].append(generated_score.item())

    def _fit_coders(self, train, metrics):
        """Fit the encoder and the decoder on the train images."""
        for cover, _ in tqdm(train, disable=not self.fit_log):
            gc.collect()
            generated, payload, decoded = self._encode_decode(cover)
            encoder_mse, decoder_loss, decoder_acc = self._coding_scores(
                cover, generated, payload, decoded)
            generated_score = self._critic(generated)

            self.decoder_optimizer.zero_grad()
            (100.0 * encoder_mse + decoder_loss + generated_score).backward()
            self.decoder_optimizer.step()

            metrics['train.encoder_mse'].append(encoder_mse.item())
            metrics['train.decoder_loss'].append(decoder_loss.item())
            metrics['train.decoder_acc'].append(decoder_acc.item())

    def _coding_scores(self, cover, generated, payload, decoded):
        encoder_mse = mse_loss(generated, cover)
        decoder_loss = binary_cross_entropy_with_logits(decoded, payload)
        decoder_acc = (decoded >= 0.0).eq(payload >= 0.5).sum().float() / payload.numel()

        return encoder_mse, decoder_loss, decoder_acc

    def _validate(self, validate, metrics):
        """Validation process"""
        for cover, _ in tqdm(validate, disable=not self.fit_log):
            gc.collect()
            generated, payload, decoded = self._encode_decode(cover, quantize=True)
            encoder_mse, decoder_loss, decoder_acc = self._coding_scores(
                cover, generated, payload, decoded)
            generated_score = self._critic(generated)
            cover_score = self._critic(cover)

            metrics['val.encoder_mse'].append(encoder_mse.item())
            metrics['val.decoder_loss'].append(decoder_loss.item())
            metrics['val.decoder_acc'].append(decoder_acc.item())
            metrics['val.cover_score'].append(cover_score.item())
            metrics['val.generated_score'].append(generated_score.item())
            metrics['val.ssim'].append(ssim(cover, generated).item())
            metrics['val.psnr'].append(10 * torch.log10(4 / encoder_mse).item())
            metrics['val.bpp'].append(self.data_depth * (2 * decoder_acc.item() - 1))

    def _generate_samples(self, cover, epoch):
        generated, payload, decoded = self._encode_decode(cover)
        samples_path = os.path.join(self.train_path, 'samples')
        samples = generated.size(0)
        for sample in range(samples):
            cover_path = os.path.join(samples_path, '{}.cover.png'.format(sample))
            sample_name = '{}.generated-{:2d}.png'.format(sample, epoch)
            sample_path = os.path.join(samples_path, sample_name)

            image = (cover[sample].permute(1, 2, 0).detach().cpu().numpy() + 1.0) / 2.0
            imageio.imwrite(cover_path, (255.0 * image).astype('uint8'))

            sampled = generated[sample].clamp(-1.0, 1.0).permute(1, 2, 0)
            sampled = sampled.detach().cpu().numpy() + 1.0

            image = sampled / 2.0
            imageio.imwrite(sample_path, (255.0 * image).astype('uint8'))

    def fit(self, train, validate, epochs=5):
        """Train a new model with the given ImageLoader class."""

        fit_id = str(uuid4())[:8]
        print('Starting fit {}'.format(fit_id))

        # In case we changed the device
        self.encoder.to(self.device)
        self.decoder.to(self.device)
        self.critic.to(self.device)

        if self.critic_optimizer is None:
            self.critic_optimizer, self.decoder_optimizer = self._get_optimizers()
            self.epochs = 0

        sample_cover = next(iter(validate))[0]
        self._create_folders()  # Needed for sampling

        if self.fit_log:
            history = list()

        # Start training
        total = self.epochs + epochs
        for epoch in range(1, epochs + 1):
            # Count how many epochs we have trained for this steganogan
            self.epochs += 1

            metrics = {field: list() for field in METRIC_FIELDS}

            if self.fit_log:
                print('Epoch {}/{}'.format(self.epochs, total))

            self._fit_critic(train, metrics)
            self._fit_coders(train, metrics)
            self._validate(validate, metrics)
            self._generate_samples(sample_cover, epoch)

            # Logging
            self.train_metrics = {k: sum(v) / len(v) for k, v in metrics.items()}
            self.train_metrics['epoch'] = epoch

            if self.fit_log:
                print(self.train_metrics)
                history.append(metrics)
                train_name = '/train.log'
                with open(self.train_path + train_name, 'wt') as fout:
                    fout.write(json.dumps(history, indent=4))

                save_name = '{}.{}.acc-{:03f}.p'.format(
                    fit_id, self.epochs, self.train_metrics['val.decoder_acc'])

                self.save(os.path.join(self.train_path, save_name))

                sv_dir = 'weights/{}t'.format(save_name)

                save_dir = os.path.join(self.train_path, sv_dir)
                torch.save((self.encoder, self.decoder, self.critic), save_dir)

            # Empty cuda cache (this may help for memory leaks)
            if self.cuda:
                torch.cuda.empty_cache()

    def save(self, path):
        """Save the fitted model in the given path. Raises an exception if there is no model."""
        with open(path, 'wb') as pickle_file:
            pickle.dump(self, pickle_file)

    @classmethod
    def load(cls, path):
        """Loads an instance of SteganoGAN from the given path."""
        with open(path, 'rb') as pickle_file:
            return pickle.load(pickle_file)
