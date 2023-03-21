#python cartoonize.py --input_dir input_images --output_dir result_images --styles shikai
#curl -X POST -F image=@input_images/temple.jpg 'http://localhost:3700/transfer' --output transformed.jpg
#docker build -t cartoonize:latest .
#docker run --name image-cartoon -d -p 3700:3700 cartoonize:latest
#docker logs image-cartoon
#docker rm -f image-cartoon


import os
import PIL
from PIL import Image
import io
import sys
import glob
import imageio
import logging
import argparse
import numpy as np
from tqdm import tqdm
from datetime import datetime
from style_transfer.cartoongan import cartoongan
import flask


#CPU
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

# initialize our Flask application and the Keras model
app = flask.Flask(__name__)

STYLES = ["shinkai", "hayao", "hosoda", "paprika"]
VALID_EXTENSIONS = ['jpg', 'png', 'gif', 'JPG']


parser = argparse.ArgumentParser(description="transform real world images to specified cartoon style(s)")
parser.add_argument("--styles", nargs="+", default=[STYLES[0]],
                    help="specify (multiple) cartoon styles which will be used to transform input images.")
parser.add_argument("--all_styles", action="store_true",
                    help="set true if all styled results are desired")
parser.add_argument("--input_dir", type=str, default="input_images",
                    help="directory with images to be transformed")
parser.add_argument("--output_dir", type=str, default="output_images",
                    help="directory where transformed images are saved")
parser.add_argument("--batch_size", type=int, default=1,
                    help="number of images that will be transformed in parallel to speed up processing. "
                         "higher value like 4 is recommended if there are gpus.")
parser.add_argument("--ignore_gif", action="store_true",
                    help="transforming gif images can take long time. enable this when you want to ignore gifs")
parser.add_argument("--overwrite", action="store_true",
                    help="enable this if you want to regenerate outputs regardless of existing results")
parser.add_argument("--skip_comparison", action="store_true",
                    help="enable this if you only want individual style result and to save processing time")
parser.add_argument("--comparison_view", type=str, default="smart",
                    choices=["smart", "horizontal", "vertical", "grid"],
                    help="specify how input images and transformed images are concatenated for easier comparison")
parser.add_argument("--gif_frame_frequency", type=int, default=1,
                    help="how often should a frame in gif be transformed. freq=1 means that every frame "
                         "in the gif will be transformed by default. set higher frequency can save processing "
                         "time while make the transformed gif less smooth")
parser.add_argument("--max_num_frames", type=int, default=100,
                    help="max number of frames that will be extracted from a gif. set higher value if longer gif "
                         "is needed")
parser.add_argument("--keep_original_size", action="store_true",
                    help="by default the input images will be resized to reasonable size to prevent potential large "
                         "computation and to save file sizes. Enable this if you want the original image size.")
parser.add_argument("--max_resized_height", type=int, default=300,
                    help="specify the max height of a image after resizing. the resized image will have the same"
                         "aspect ratio. Set higher value or enable `keep_original_size` if you want larger image.")
parser.add_argument("--convert_gif_to_mp4", action="store_true",
                    help="convert transformed gif to mp4 which is much more smaller and easier to share. "
                         "`ffmpeg` need to be installed at first.")
parser.add_argument("--logging_lvl", type=str, default="info",
                    choices=["debug", "info", "warning", "error", "critical"],
                    help="logging level which decide how verbosely the program will be. set to `debug` if necessary")
parser.add_argument("--debug", action="store_true",
                    help="show the most detailed logging messages for debugging purpose")
parser.add_argument("--show_tf_cpp_log", action="store_true")

args = parser.parse_args()

TEMPORARY_DIR = os.path.join(f"{args.output_dir}", ".tmp")


logger = logging.getLogger("Cartoonizer")
logger.propagate = False
log_lvl = {"debug": logging.DEBUG, "info": logging.INFO,
           "warning": logging.WARNING, "error": logging.ERROR,
           "critical": logging.CRITICAL}
if args.debug:
    logger.setLevel(logging.DEBUG)
else:
    logger.setLevel(log_lvl[args.logging_lvl])
formatter = logging.Formatter(
    "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
stdhandler = logging.StreamHandler(sys.stdout)
stdhandler.setFormatter(formatter)
logger.addHandler(stdhandler)

if not args.show_tf_cpp_log:
    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"


def pre_processing(image_path, style, expand_dim=True):
    input_image = PIL.Image.open(image_path).convert("RGB")

    if not args.keep_original_size:
        width, height = input_image.size
        aspect_ratio = width / height
        resized_height = min(height, args.max_resized_height)
        resized_width = int(resized_height * aspect_ratio)
        if width != resized_width:
            logger.debug(f"resized ({width}, {height}) to: ({resized_width}, {resized_height})")
            input_image = input_image.resize((resized_width, resized_height))

    input_image = np.asarray(input_image)
    input_image = input_image.astype(np.float32)

    input_image = input_image[:, :, [2, 1, 0]]

    if expand_dim:
        input_image = np.expand_dims(input_image, axis=0)
    return input_image


def post_processing(transformed_image, style):
    if not type(transformed_image) == np.ndarray:
        transformed_image = transformed_image.numpy()
    transformed_image = transformed_image[0]
    transformed_image = transformed_image[:, :, [2, 1, 0]]
    transformed_image = transformed_image * 0.5 + 0.5
    transformed_image = transformed_image * 255
    return transformed_image


def save_transformed_image(output_image, img_filename, save_dir):
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    transformed_image_path = os.path.join(save_dir, img_filename)

    if output_image is not None:
        image = PIL.Image.fromarray(output_image.astype("uint8"))
        if os.path.exists("/home/result/output.jpg"):
            os.remove("/home/result/output.jpg")
        image.save("/home/result/output.jpg")

    return transformed_image_path


def save_concatenated_image(image_paths, image_folder="comparison", num_columns=2):
    images = [PIL.Image.open(i).convert('RGB') for i in image_paths]
    # pick the image which is the smallest, and resize the others to match it (can be arbitrary image shape here)
    min_shape = sorted([(np.sum(i.size), i.size) for i in images])[0][1]
    array = np.asarray([np.asarray(i.resize(min_shape)) for i in images])

    view = args.comparison_view
    if view == "smart":
        width, height = min_shape[0], min_shape[1]
        aspect_ratio = width / height
        logger.debug(f"(width, height): ({width}, {height}), aspect_ratio: {aspect_ratio}")
        grid_suitable = (len(args.styles) + 1) % num_columns == 0
        is_portrait = aspect_ratio <= 0.75
        if grid_suitable and not is_portrait:
            view = "grid"
        elif is_portrait:
            view = "horizontal"
        else:
            view = "vertical"

    if view == "horizontal":
        images_comb = np.hstack(array)
    elif view == "vertical":
        images_comb = np.vstack(array)
    elif view == "grid":
        rows = np.split(array, num_columns)
        rows = [np.hstack(row) for row in rows]
        images_comb = np.vstack([row for row in rows])
    else:
        logger.debug(f"Wrong `comparison_view`: {args.comparison_view}")

    images_comb = PIL.Image.fromarray(images_comb)
    file_name = image_paths[0].split("/")[-1]

    if args.output_dir not in image_folder:
        image_folder = os.path.join(args.output_dir, image_folder)
    if not os.path.exists(image_folder):
        os.makedirs(image_folder)

    image_path = os.path.join(image_folder, file_name)
    images_comb.save(image_path)
    return image_path


def convert_gif_to_png(gif_path):
    logger.debug(f"`{gif_path}` is a gif, extracting png images from it...")
    gif_filename = gif_path.split("/")[-1].replace(".gif", "")
    image = PIL.Image.open(gif_path)
    palette = image.getpalette()
    png_paths = list()
    i = 0

    png_dir = os.path.join(TEMPORARY_DIR, gif_filename)
    if not os.path.exists(png_dir):
        logger.debug(f"Creating temporary folder: {png_dir} for storing intermediate result...")
        os.makedirs(png_dir)

    prev_generated_png_paths = glob.glob(png_dir + '/*.png')
    if prev_generated_png_paths:
        return prev_generated_png_paths

    num_processed_frames = 0
    logger.debug("Generating png images...")
    try:
        while num_processed_frames < args.max_num_frames:

            image.putpalette(palette)
            extracted_image = PIL.Image.new("RGB", image.size)
            extracted_image.paste(image)

            if not args.keep_original_size:
                width, height = extracted_image.size
                aspect_ratio = width / height
                resized_height = min(height, args.max_resized_height)
                resized_width = int(resized_height * aspect_ratio)
                if width != resized_width:
                    logger.debug(f"resized ({width}, {height}) to: ({resized_width}, {resized_height})")
                    extracted_image = extracted_image.resize((resized_width, resized_height))

            if i % args.gif_frame_frequency == 0:
                png_filename = f"{i + 1}.png"
                png_path = os.path.join(png_dir, png_filename)
                extracted_image.save(png_path)
                png_paths.append(png_path)
                num_processed_frames += 1

            image.seek(image.tell() + 1)
            i += 1

    except EOFError:
        pass  # end of sequence

    logger.debug(f"Number of {len(png_paths)} png images were generated at {png_dir}.")
    return png_paths


def transform_png_images(image_paths, model, style, return_existing_result=False):
    transformed_image_paths = list()
    save_dir = os.path.join("/".join(image_paths[0].split("/")[:-1]), style)
    logger.debug(f"Transforming {len(image_paths)} images and saving them to {save_dir}....")

    if return_existing_result:
        return glob.glob(os.path.join(save_dir, "*.png"))

    num_batch = int(np.ceil(len(image_paths) / args.batch_size))
    image_paths = np.array_split(image_paths, num_batch)

    logger.debug(f"Processing {num_batch} batches with batch_size={args.batch_size}...")
    for batch_image_paths in image_paths:
        image_filenames = [path.split("/")[-1] for path in batch_image_paths]
        input_images = [pre_processing(path, style=style, expand_dim=False) for path in batch_image_paths]
        input_images = np.stack(input_images, axis=0)
        transformed_images = model(input_images)
        output_images = [post_processing(image, style=style)
                         for image in np.split(transformed_images, transformed_images.shape[0])]
        paths = [save_transformed_image(img, f, save_dir)
                 for img, f in zip(output_images, image_filenames)]
        transformed_image_paths.extend(paths)

    return transformed_image_paths


def save_png_images_as_gif(image_paths, image_filename, style="comparison"):

    gif_dir = os.path.join(args.output_dir, style)
    if not os.path.exists(gif_dir):
        os.makedirs(gif_dir)
    gif_path = os.path.join(gif_dir, image_filename)

    with imageio.get_writer(gif_path, mode='I') as writer:
        file_names = sorted(image_paths, key=lambda x: int(x.split('/')[-1].replace('.png', '')))
        logger.debug(f"Combining {len(file_names)} png images into {gif_path}...")
        for i, filename in enumerate(file_names):
            image = imageio.imread(filename)
            writer.append_data(image)
    return gif_path


def convert_gif_to_mp4(gif_path, crf=25):

    mp4_dir = os.path.join(os.path.dirname(gif_path), "mp4")
    gif_file = gif_path.split("/")[-1]
    if not os.path.exists(mp4_dir):
        os.makedirs(mp4_dir)
    mp4_path = os.path.join(mp4_dir, gif_file.replace(".gif", ".mp4"))
    cmd = "ffmpeg -y -i {} -movflags faststart -pix_fmt yuv420p -vf \"scale=trunc(iw/2)*2:trunc(ih/2)*2\" -crf {} {}"
    cmd = cmd.replace("My Drive", "My\ Drive")
    os.system(cmd.format(gif_path, crf, mp4_path))


def result_exist(image_path, style):
    return os.path.exists(os.path.join(args.output_dir, style, image_path.split("/")[-1]))

@app.route("/transfer", methods=["POST"])
def transfer():
    data = dict()
    data["success"] = False
    output_dir = "/home/result/"

    if flask.request.method == "POST":
        if flask.request.files.get("image"):
            # 获取图片
            image = flask.request.files["image"]
            if os.path.exists("input.jpg"):
                os.remove("input.jpg")
            image.save("input.jpg")





            start = datetime.now()
            logger.info(f"Transformed images will be saved to `{output_dir}` folder.")
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)

            # create temporary folder which will be deleted after transformations
            if not os.path.exists(TEMPORARY_DIR):
                os.makedirs(TEMPORARY_DIR)

            # 获取转换风格
            styles = []
            styles.append("shinkai")
            models = list()
            for style in styles:
                models.append(cartoongan.load_model(style))

            logger.info(f"Cartoonizing images using {', '.join(styles)} style...")



            image_filename = "/home/input.jpg"
            if not os.path.exists(image_filename):
                data["error1"] = "no such a file named '/home/input.jpg'!"
            if not os.path.exists("/home/result"):
                data["error2"] = "no such a dictory named '/home/result'!"

            for model, style in zip(models, styles):
                input_image = pre_processing(image_filename, style=style)
                save_dir = os.path.join(output_dir, style)
                return_existing_result = result_exist(image_filename, style) and not args.overwrite
                data["success"] = True
                if not return_existing_result:
                    transformed_image = model(input_image)
                    output_image = post_processing(transformed_image, style=style)
                    if output_image is None:
                        data["error3"] = "output image is none!"

                    transformed_image_path = save_transformed_image(output_image, image_filename, save_dir)
                else:
                    transformed_image_path = save_transformed_image(None, image_filename, save_dir)
            time_elapsed = datetime.now() - start
            logger.info(f"Total processing time: {time_elapsed}")

    if os.path.exists("/home/result/output.jpg"):
        return flask.send_file("/home/result/output.jpg", mimetype="image/jpg")
    else:
        data["error"] = "no such a file named '/home/result/output.jpg'!"
        return flask.jsonify(data)






if __name__ == "__main__":
    app.run(host='0.0.0.0',port=3700,threaded=True)