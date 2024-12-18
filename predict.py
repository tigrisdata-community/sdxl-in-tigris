import os
import shutil
import subprocess
import time
import boto3
from sqids import Sqids
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from scripts.download import copy_from_tigris

import numpy as np
import torch
from cog import BasePredictor, Input, Path
from diffusers import StableDiffusionXLPipeline

# from diffusers.pipelines.stable_diffusion.safety_checker import (
#     StableDiffusionSafetyChecker,
# )
from diffusers.utils import load_image
from diffusers.image_processor import VaeImageProcessor
from transformers import CLIPImageProcessor
from PIL import ImageOps, Image
from compel import Compel, ReturnedEmbeddingsType


FLUX_MODEL_CACHE = os.getenv("MODEL_CACHE_DIR", "/src/")
FEATURE_EXTRACTOR = "./feature-extractor"
PUBLIC_BUCKET_NAME = os.getenv("PUBLIC_BUCKET_NAME")


def upload_to_s3(files: List[str], bucket_name: str):
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        endpoint_url=os.environ["AWS_ENDPOINT_URL_S3"],
        region_name=os.environ.get("AWS_REGION", None),
    )

    presigned_urls = []

    for file_path in files:
        file_name = os.path.basename(file_path)
        s3_key = f"{file_name}"

        try:
            s3_client.upload_file(file_path, bucket_name, s3_key)
            print(f"Uploaded {file_name} to S3 bucket {bucket_name} successfully.")

            presigned_url = generate_presigned_url(bucket_name, s3_key)
            if presigned_url:
                presigned_urls.append(presigned_url)
        except Exception as e:
            print(f"Error uploading {file_name} to S3: {e}")

    return presigned_urls


def generate_presigned_url(bucket_name: str, object_name: str, expiration: int = 3600):
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        endpoint_url=os.environ["AWS_ENDPOINT_URL_S3"],
        region_name=os.environ.get("AWS_REGION", None),
    )

    try:
        response = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name, "Key": object_name},
            ExpiresIn=expiration,
        )
    except Exception as e:
        print(f"Error generating presigned URL: {e}")
        return None

    return response


def resize_image_dimensions(
    original_resolution_wh: Tuple[int, int],
    maximum_dimension: int = 1024
) -> Tuple[int, int]:
    width, height = original_resolution_wh

    if width > height:
        scaling_factor = maximum_dimension / width
    else:
        scaling_factor = maximum_dimension / height

    new_width = int(width * scaling_factor)
    new_height = int(height * scaling_factor)

    new_width = new_width - (new_width % 32)
    new_height = new_height - (new_height % 32)

    return new_width, new_height


class Predictor(BasePredictor):
    def setup(self):
        """Load the model into memory to make running multiple predictions efficient"""

        assert PUBLIC_BUCKET_NAME is not None, "set PUBLIC_BUCKET_NAME to your image upload bucket"

        start = time.time()

        self.feature_extractor = CLIPImageProcessor.from_pretrained(FEATURE_EXTRACTOR)

        model_path = FLUX_MODEL_CACHE + os.getenv("MODEL_PATH")

        if not os.path.exists(FLUX_MODEL_CACHE + "/model_index.json"):
            print("Downloading model")
            model_path = copy_from_tigris(destdir=FLUX_MODEL_CACHE)

        print("Loading sdxl txt2img pipeline...")
        dtype = torch.bfloat16

        self.txt2img_pipe = StableDiffusionXLPipeline.from_pretrained(
            model_path, torch_dtype=dtype,
        ).to("cuda")

        self.compel = Compel(tokenizer=[self.txt2img_pipe.tokenizer, self.txt2img_pipe.tokenizer_2] , text_encoder=[self.txt2img_pipe.text_encoder, self.txt2img_pipe.text_encoder_2], returned_embeddings_type=ReturnedEmbeddingsType.PENULTIMATE_HIDDEN_STATES_NON_NORMALIZED, requires_pooled=[False, True])

        print("setup took: ", time.time() - start)

    def aspect_ratio_to_width_height(self, aspect_ratio: str):
        aspect_ratios = {
            "1:1": (1024, 1024),
            "16:9": (1344+64, 768+64),
            "21:9": (1536, 640),
            "3:2": (1216, 832),
            "2:3": (832, 1216),
            "4:3": (800, 600),
            "3:4": (600, 800),
            "4:5": (896, 1088),
            "5:4": (1088, 896),
            "9:16": (768, 1344),
            "9:21": (640, 1536),
        }
        return aspect_ratios.get(aspect_ratio)

    @torch.inference_mode()
    def predict(
        self,
        prompt: str = Input(
            description="Input prompt",
            default="",
        ),
        negative_prompt: str = Input(
            description="Input negative prompt",
            default="",
        ),
        aspect_ratio: str = Input(
            description="Aspect ratio for the generated image",
            choices=["1:1", "16:9", "21:9", "2:3", "3:2", "4:5", "5:4", "9:16", "9:21"],
            default="1:1",
        ),
        num_outputs: int = Input(
            description="Number of images to output.",
            ge=1,
            le=3,
            default=1,
        ),
        guidance_scale: float = Input(
            description="Scale for classifier-free guidance", ge=0, le=50, default=0.0
        ),
        max_sequence_length: int = Input(
            description="Max sequence length", ge=1, le=2048, default=256
        ),
        num_inference_steps: int = Input(
            description="Number of inference steps", ge=1, le=100, default=50
        ),
        prompt_strength: float = Input(
            description="Prompt strength when using img2img. 1.0 corresponds to full destruction of information in image",
            ge=0.0,
            le=1.0,
            default=0.6,
        ),
        seed: int = Input(
            description="Random seed. Leave blank to randomize the seed", default=None
        ),
        output_format: str = Input(
            description="Format of the output images",
            choices=["webp", "jpg", "png"],
            default="webp",
        ),
        output_quality: int = Input(
            description="Quality when saving the output images, from 0 to 100. 100 is best quality, 0 is lowest quality. Not relevant for .png outputs",
            default=80,
            ge=0,
            le=100,
        ),
    ) -> List[Path]:
        """Run a single prediction on the model."""
        if seed is None:
            seed = int.from_bytes(os.urandom(2), "big")
        print(f"Using seed: {seed}")

        width, height = self.aspect_ratio_to_width_height(aspect_ratio)

        flux_kwargs = {}

        width, height = resize_image_dimensions((width, height))
        flux_kwargs["width"] = width
        flux_kwargs["height"] = height
        pipe = self.txt2img_pipe

        generator = torch.Generator("cuda").manual_seed(seed)

        conditioning, pooled = self.compel([prompt, negative_prompt])

        common_args = {
            "prompt_embeds": conditioning[0:1] * num_outputs,
            "negative_prompt_embeds": conditioning[1:2] * num_outputs,
            "pooled_prompt_embeds": pooled[0:1] * num_outputs,
            "negative_pooled_prompt_embeds": pooled[1:2] * num_outputs,
            "guidance_scale": guidance_scale,
            "max_sequence_length": max_sequence_length,
            "generator": generator,
            "num_inference_steps": num_inference_steps,
        }

        output = pipe(**common_args, **flux_kwargs)

        output_paths = []
        sqids = Sqids()
        current_timestamp = int(time.time())

        for i, image in enumerate(output.images):
            unique_id = sqids.encode([current_timestamp, i])
            output_path = f"/tmp/out-{unique_id}.{output_format}"
            if output_format != "png":
                image.save(output_path, quality=output_quality, optimize=True)
            else:
                image.save(output_path)
            output_paths.append(Path(output_path))

        if len(output_paths) == 0:
            raise Exception(
                "Something went wrong. Try running it again, or try a different prompt."
            )

        return upload_to_s3(output_paths, PUBLIC_BUCKET_NAME)