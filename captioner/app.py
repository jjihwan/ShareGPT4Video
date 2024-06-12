import base64
import os
import shutil
import tempfile
from io import BytesIO

import gradio as gr
import numpy as np
import torch
import torchvision.transforms as transforms
from decord import VideoReader
from PIL import Image, ImageDraw, ImageFont
from transformers import AutoModel, AutoTokenizer

import spaces

title_markdown = ("""
<div style="display: flex; justify-content: flex-start; align-items: center; text-align: center;">
  <div style="margin-right: 20px; display: flex; align-items: center;">
    <a href="https://github.com/ShareGPT4Omni/ShareGPT4Video" style="text-decoration: none; display: flex; align-items: center;">
      <img src="https://raw.githubusercontent.com/ShareGPT4V/ShareGPT4V-Resources/master/images/share4video_tight.png" alt="ShareGPT4Video🚀" style="max-width: 120px; height: auto;">
    </a>
  </div>
  <div>
    <h1>ShareGPT4Video: Improving Video Understanding and Generation with Better Captions</h1>
    <h5 style="margin: 0;">If you like our project, please give us a star ✨ on Github for the latest update.</h5>
    <h5 style="margin: 0;"> <a href="https://sharegpt4video.github.io/">[Project Page]</a> <a href="https://github.com/ShareGPT4Omni/ShareGPT4Video">[Code]</a> <a href="https://arxiv.org/abs/2406.04325v1">[Paper]</a>
  </div>
</div>
""")

block_css = """
#buttons button {
    min-width: min(120px,100%);
}
"""

learn_more_markdown = ("""
### License
The service is a research preview intended for non-commercial use only, subject to the model [License](https://github.com/facebookresearch/llama/blob/main/MODEL_CARD.md) of LLaMA, [Terms of Use](https://openai.com/policies/terms-of-use) of the data generated by OpenAI, and [Privacy Practices](https://chrome.google.com/webstore/detail/sharegpt-share-your-chatg/daiacboceoaocpibfodeljbdfacokfjb) of ShareGPT. Please contact us if you find any potential violation.
""")


new_path = 'Lin-Chen/ShareCaptioner-Video'
tokenizer = AutoTokenizer.from_pretrained(new_path, trust_remote_code=True)
model = AutoModel.from_pretrained(
    new_path, torch_dtype=torch.float16, trust_remote_code=True).cuda().eval()
model.cuda()
model.tokenizer = tokenizer


def padding_336(b, pad=336):
    width, height = b.size
    tar = int(np.ceil(height / pad) * pad)
    top_padding = int((tar - height)/2)
    bottom_padding = tar - height - top_padding
    left_padding = 0
    right_padding = 0
    b = transforms.functional.pad(
        b, [left_padding, top_padding, right_padding, bottom_padding], fill=[255, 255, 255])

    return b


def HD_transform(img, hd_num=25):
    width, height = img.size
    trans = False
    if width < height:
        img = img.transpose(Image.TRANSPOSE)
        trans = True
        width, height = img.size
    ratio = (width / height)
    scale = 1
    while scale*np.ceil(scale/ratio) <= hd_num:
        scale += 1
    scale -= 1
    new_w = int(scale * 336)
    new_h = int(new_w / ratio)

    img = transforms.functional.resize(img, [new_h, new_w],)
    img = padding_336(img, 336)
    width, height = img.size
    if trans:
        img = img.transpose(Image.TRANSPOSE)

    return img


def get_seq_frames(total_num_frames, desired_num_frames, start=None, end=None):
    if start is None:
        assert end is None
        start, end = 0, total_num_frames
    print(f"{start=}, {end=}")
    desired_num_frames -= 2
    end = min(total_num_frames, end)
    start = max(start, 0)
    seg_size = float((end - start)) / desired_num_frames
    seq = [start]

    for i in range(desired_num_frames):
        s = int(np.round(seg_size * i))
        e = int(np.round(seg_size * (i + 1)))
        seq.append(min(int(start + (s + e) // 2), total_num_frames-1))
    return seq + [end-1]


def model_gen(model, text, images, need_bos=True, hd_num=25, max_new_token=2048, beam=3, do_sample=False):
    pt1 = 0
    embeds = []
    im_mask = []
    if images is None:
        images = []
        images_loc = []
    else:
        images = [images]
        images_loc = [0]
    for i, pts in enumerate(images_loc + [len(text)]):
        subtext = text[pt1:pts]
        if need_bos or len(subtext) > 0:
            text_embeds = model.encode_text(
                subtext, add_special_tokens=need_bos)
            embeds.append(text_embeds)
            im_mask.append(torch.zeros(text_embeds.shape[:2]).cuda())
            need_bos = False
        if i < len(images):
            try:
                image = Image.open(images[i]).convert('RGB')
            except:
                image = images[i].convert('RGB')

            image = HD_transform(image, hd_num=hd_num)
            image = model.vis_processor(image).unsqueeze(0).cuda()
            image_embeds = model.encode_img(image)
            print(image_embeds.shape)
            embeds.append(image_embeds)
            im_mask.append(torch.ones(image_embeds.shape[:2]).cuda())
        pt1 = pts
    embeds = torch.cat(embeds, dim=1)
    im_mask = torch.cat(im_mask, dim=1)
    im_mask = im_mask.bool()
    outputs = model.generate(inputs_embeds=embeds, im_mask=im_mask,
                             temperature=1.0, max_new_tokens=max_new_token, num_beams=beam,
                             do_sample=False, repetition_penalty=1.00)

    output_token = outputs[0]
    if output_token[0] == 0 or output_token[0] == 1:
        output_token = output_token[1:]
    output_text = model.tokenizer.decode(
        output_token, add_special_tokens=False)
    output_text = output_text.split('[UNUSED_TOKEN_145]')[0].strip()
    output_text = output_text.split('<|im_end|>')[0].strip()
    return output_text


def img_process(imgs):
    new_w = 0
    new_h = 0
    for im in imgs:
        w, h = im.size
        new_w = max(new_w, w)
        new_h += h + 20
    pad = max(new_w // 4, 100)
    new_w += 20
    new_h += 20
    font = ImageFont.truetype("SimHei.ttf", pad // 5)
    new_img = Image.new('RGB', (new_w + pad, new_h), 'white')
    draw = ImageDraw.Draw(new_img)
    curr_h = 10
    for idx, im in enumerate(imgs):
        w, h = im.size
        new_img.paste(im, (pad, curr_h))
        draw.text((0, curr_h + h // 2),
                  f'<IMAGE {idx}>', font=font, fill='black')
        if idx + 1 < len(imgs):
            draw.line([(0, curr_h + h + 10), (new_w+pad,
                      curr_h + h + 10)], fill='black', width=2)
        curr_h += h + 20
    return new_img


def load_quota_video(vis_path, start=None, end=None):
    vr = VideoReader(vis_path)
    total_frame_num = len(vr)
    fps = vr.get_avg_fps()
    if start is not None:
        assert end is not None
        start_frame = int(start * fps)
        end_frame = min(int(end * fps), total_frame_num)
    else:
        start_frame = 0
        end_frame = total_frame_num
    interval = int(2 * fps)
    frame_idx = list(range(start_frame, end_frame, interval))
    img_array = vr.get_batch(frame_idx).asnumpy()
    num_frm, H, W, _ = img_array.shape
    img_array = img_array.reshape(
        (1, num_frm, img_array.shape[-3], img_array.shape[-2], img_array.shape[-1]))
    clip_imgs = []
    for j in range(num_frm):
        clip_imgs.append(Image.fromarray(img_array[0, j]))
    return clip_imgs


def resize_image(image_path, max_size=1024):
    with Image.open(image_path) as img:
        width, height = img.size
        if width > max_size or height > max_size:
            if width > height:
                new_width = max_size
                new_height = int(height * (max_size / width))
            else:
                new_height = max_size
                new_width = int(width * (max_size / height))
        else:
            new_width = width
            new_height = height
        resized_img = img.resize((new_width, new_height))
        print(f"resized_img_size: {resized_img.size}")
        return resized_img


def encode_resized_image(image_path, max_size=1024):
    resized_img = resize_image(image_path, max_size)
    try:
        with BytesIO() as buffer:
            resized_img.save(buffer, format="JPEG")
            return base64.b64encode(buffer.getvalue()).decode('utf-8')
    except:
        with BytesIO() as buffer:
            rgb_img = resized_img.convert('RGB')
            rgb_img.save(buffer, format="JPEG")
            return base64.b64encode(buffer.getvalue()).decode('utf-8')


@spaces.GPU(duration=60)
def generate_slidingcaptioning(video_path):
    imgs = load_quota_video(video_path)
    q = 'This is the first frame of a video, describe it in detail.'
    query = f'[UNUSED_TOKEN_146]user\n{q}[UNUSED_TOKEN_145]\n[UNUSED_TOKEN_146]assistant\n'
    img = imgs[0]
    with torch.cuda.amp.autocast():
        response = model_gen(model, query, img, hd_num=9)
    print(response)
    responses = [response]
    images = [img]
    for idx in range(len(imgs)-1):
        image1 = imgs[idx]
        image2 = imgs[idx+1]
        prompt = "Here are the Video frame {} at {}.00 Second(s) and Video frame {} at {}.00 Second(s) of a video, describe what happend between them. What happend before is: {}".format(
            idx, int(idx*2), idx+1, int((idx+1)*2), response)
        width, height = image1.size
        new_img = Image.new('RGB', (width, 2*height+50), 'white')
        new_img.paste(image1, (0, 0))
        new_img.paste(image2, (0, height+50))
        query = f'[UNUSED_TOKEN_146]user\n{prompt}[UNUSED_TOKEN_145]\n[UNUSED_TOKEN_146]assistant\n'
        with torch.cuda.amp.autocast():
            response = model_gen(model, query, new_img, hd_num=9)
        responses.append(response)
        images.append(new_img)
    prompt = 'Summarize the following per frame descriptions:\n'
    for idx, txt in enumerate(responses):
        prompt += 'Video frame {} at {}.00 Second(s) description: {}\n'.format(
            idx+1, idx*2, txt)
    query = f'[UNUSED_TOKEN_146]user\n{prompt}[UNUSED_TOKEN_145]\n[UNUSED_TOKEN_146]assistant\n'
    print(query)
    with torch.cuda.amp.autocast():
        summ = model_gen(model, query, None, hd_num=16)
    print(summ)
    return summ


@spaces.GPU(duration=60)
def generate_fastcaptioning(video_path):
    q = 'Here are a few key frames of a video, discribe this video in detail.'
    query = f'[UNUSED_TOKEN_146]user\n{q}[UNUSED_TOKEN_145]\n[UNUSED_TOKEN_146]assistant\n'
    imgs = load_quota_video(video_path)
    img = img_process(imgs)
    with torch.cuda.amp.autocast():
        response = model_gen(model, query, img, hd_num=16,
                             do_sample=False, beam=3)
    return response


@spaces.GPU(duration=60)
def generate_promptrecaptioning(text):
    q = f'Translate this brief generation prompt into a detailed caption: {text}'
    query = f'[UNUSED_TOKEN_146]user\n{q}[UNUSED_TOKEN_145]\n[UNUSED_TOKEN_146]assistant\n'
    with torch.cuda.amp.autocast():
        response = model_gen(model, query, None)
    return response


def save_video_to_local(video_path):
    filename = os.path.join('temp', next(
        tempfile._get_candidate_names()) + '.mp4')
    shutil.copyfile(video_path, filename)
    return filename


with gr.Blocks(title='ShareCaptioner-Video', theme=gr.themes.Default(), css=block_css) as demo:
    gr.Markdown(title_markdown)
    state = gr.State()
    state_ = gr.State()
    first_run = gr.State()

    with gr.Row():
        gr.Markdown("### The ShareCaptioner-Video is a Four-in-One exceptional video captioning model with the following capabilities:\n1. Fast captioning, 2. Sliding Captioning, 3. Clip Summarizing, 4. Prompt Re-Captioning")
    with gr.Row():
        gr.Markdown("(THE DEMO OF \"Clip Summarizing\" IS COMING SOON...)")
    with gr.Row():
        with gr.Column(scale=6):
            with gr.Row():
                video = gr.Video(label="Input Video")
            with gr.Row():
                textbox = gr.Textbox(
                    show_label=False, placeholder="Input Text", container=False
                )
            with gr.Row():
                with gr.Column(scale=2, min_width=50):
                    submit_btn_sc = gr.Button(
                        value="Sliding Captioning", variant="primary", interactive=True
                    )
                with gr.Column(scale=2, min_width=50):
                    submit_btn_fc = gr.Button(
                        value="Fast Captioning", variant="primary", interactive=True
                    )
                with gr.Column(scale=2, min_width=50):
                    submit_btn_pr = gr.Button(
                        value="Prompt Re-captioning", variant="primary", interactive=True
                    )
        with gr.Column(scale=4, min_width=200):
            with gr.Row():
                textbox_out = gr.Textbox(
                    show_label=False, placeholder="Output", container=False
                )
    gr.Markdown(learn_more_markdown)

    submit_btn_sc.click(generate_slidingcaptioning, [video], [textbox_out])
    submit_btn_fc.click(generate_fastcaptioning, [video], [textbox_out])
    submit_btn_pr.click(generate_promptrecaptioning, [textbox], [textbox_out])

demo.launch()