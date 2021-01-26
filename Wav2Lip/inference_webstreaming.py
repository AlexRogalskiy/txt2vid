# Based on https://www.pyimagesearch.com/2019/09/02/opencv-stream-video-to-web-browser-html-page/
# Basic explanation: https://towardsdatascience.com/video-streaming-in-web-browsers-with-opencv-flask-93a38846fe00

from os import listdir, path
import numpy as np
import scipy, cv2, os, sys, argparse, audio
import json, subprocess, random, string
from tqdm import tqdm
from glob import glob
import torch, face_detection
from models import Wav2Lip
import platform
from flask import Response, Flask, render_template
import threading
import argparse
import datetime
import time
import cv2


parser = argparse.ArgumentParser(description='Inference code to lip-sync videos in the wild using Wav2Lip models')

parser.add_argument('--checkpoint_path', type=str,
					help='Name of saved checkpoint to load weights from', required=True)

parser.add_argument('--face', type=str,
					help='Filepath of video/image that contains faces to use', required=True)
parser.add_argument('--audio', type=str,
					help='Filepath of video/audio file to use as raw audio source', required=True)
# parser.add_argument('--outfile', type=str, help='Video path to save result. See default for an e.g.',
# 					default='results/result_voice.mp4')

parser.add_argument('--static', type=bool,
					help='If True, then use only first video frame for inference', default=False)
parser.add_argument('--fps', type=float, help='Can be specified only if input is a static image (default: 25)',
					default=25., required=False)

parser.add_argument('--pads', nargs='+', type=int, default=[0, 10, 0, 0],
					help='Padding (top, bottom, left, right). Please adjust to include chin at least')

parser.add_argument('--face_det_batch_size', type=int,
					help='Batch size for face detection', default=16)
parser.add_argument('--wav2lip_batch_size', type=int, help='Batch size for Wav2Lip model(s)', default=128)

parser.add_argument('--resize_factor', default=1, type=int,
					help='Reduce the resolution by this factor. Sometimes, best results are obtained at 480p or 720p')

parser.add_argument('--crop', nargs='+', type=int, default=[0, -1, 0, -1],
					help='Crop video to a smaller region (top, bottom, left, right). Applied after resize_factor and rotate arg. '
						 'Useful if multiple face present. -1 implies the value will be auto-inferred based on height, width')

parser.add_argument('--box', nargs='+', type=int, default=[-1, -1, -1, -1],
					help='Specify a constant bounding box for the face. Use only as a last resort if the face is not detected.'
						 'Also, might work only if the face is not moving around much. Syntax: (top, bottom, left, right).')

parser.add_argument('--rotate', default=False, action='store_true',
					help='Sometimes videos taken from a phone can be flipped 90deg. If true, will flip video right by 90deg.'
						 'Use if you get a flipped result, despite feeding a normal looking video')

parser.add_argument('--nosmooth', default=False, action='store_true',
					help='Prevent smoothing face detections over a short temporal window')

# IP and Port for Video Streaming
parser.add_argument("-i", "--ip", type=str, default="0.0.0.0", #172.24.92.25
		help="ip address of the device")
parser.add_argument("-o", "--port", type=int, default=8000,
		help="ephemeral port number of the server (1024 to 65535)")

args = parser.parse_args()
args.img_size = 96

# initialize the output frame and a lock used to ensure thread-safe
# exchanges of the output frames (useful when multiple browsers/tabs
# are viewing the stream)
outputFrame = None
lock = threading.Lock()
# initialize a flask object
app = Flask(__name__)


if os.path.isfile(args.face) and args.face.split('.')[1] in ['jpg', 'png', 'jpeg']:
	args.static = True

def get_smoothened_boxes(boxes, T):
	for i in range(len(boxes)):
		if i + T > len(boxes):
			window = boxes[len(boxes) - T:]
		else:
			window = boxes[i: i + T]
		boxes[i] = np.mean(window, axis=0)
	return boxes

def face_detect(images):
	detector = face_detection.FaceAlignment(face_detection.LandmarksType._2D,
											flip_input=False, device=device)

	batch_size = args.face_det_batch_size

	while 1:
		predictions = []
		try:
			for i in range(0, len(images), batch_size):
				predictions.extend(detector.get_detections_for_batch(np.array(images[i:i + batch_size])))
		except RuntimeError:
			if batch_size == 1:
				raise RuntimeError(
					'Image too big to run face detection on GPU. Please use the --resize_factor argument')
			batch_size //= 2
			print('Recovering from OOM error; New batch size: {}'.format(batch_size))
			continue
		break

	results = []
	pady1, pady2, padx1, padx2 = args.pads
	for rect, image in zip(predictions, images):
		if rect is None:
			cv2.imwrite('temp/faulty_frame.jpg', image)  # check this frame where the face was not detected.
			raise ValueError('Face not detected! Ensure the video contains a face in all the frames.')

		y1 = max(0, rect[1] - pady1)
		y2 = min(image.shape[0], rect[3] + pady2)
		x1 = max(0, rect[0] - padx1)
		x2 = min(image.shape[1], rect[2] + padx2)

		results.append([x1, y1, x2, y2])

	boxes = np.array(results)
	if not args.nosmooth: boxes = get_smoothened_boxes(boxes, T=5)
	results = [[image[y1: y2, x1:x2], (y1, y2, x1, x2)] for image, (x1, y1, x2, y2) in zip(images, boxes)]

	del detector
	return results

def face_detect_wrapper(frames):
    if args.box[0] == -1:
        if not args.static:
            face_det_results = face_detect(frames)  # BGR2RGB for CNN face detection
        else:
            face_det_results = face_detect([frames[0]])
    else:
        print('Using the specified bounding box instead of face detection...')
        y1, y2, x1, x2 = args.box
        face_det_results = [[f[y1: y2, x1:x2], (y1, y2, x1, x2)] for f in frames]
    return face_det_results

def datagen(frames, face_det_results, mels, start_frame_idx):
    # start frame idx is the current frame idx in the output video
    # we start from this point
    img_batch, mel_batch, frame_batch, coords_batch = [], [], [], []

    start_frame_idx = start_frame_idx%len(frames) # loop back
    num_frames = len(mels)
    # take frames from start_frame_idx to start_frame_idx+num_frames
    # wrapping around if necessary
    if not args.static:
        if len(frames) == 1:
            frames_current = frames
            face_det_results_current = face_det_results
        if start_frame_idx + num_frames > len(frames):
            frames_current = frames[start_frame_idx:] + frames[:start_frame_idx + num_frames-len(frames)]
            face_det_results_current = face_det_results[start_frame_idx:] + face_det_results[:start_frame_idx + num_frames-len(frames)]
        else:
            frames_current = frames[start_frame_idx:start_frame_idx+num_frames]
            face_det_results_current = face_det_results[start_frame_idx:start_frame_idx+num_frames]

    else:
        frames_current = frames
        face_det_results_current = face_det_results

    for i, m in enumerate(mels):
        idx = 0 if args.static else i % len(frames_current)
        frame_to_save = frames_current[idx].copy()
        face, coords = face_det_results_current[idx].copy()

        face = cv2.resize(face, (args.img_size, args.img_size))

        img_batch.append(face)
        mel_batch.append(m)
        frame_batch.append(frame_to_save)
        coords_batch.append(coords)

        if len(img_batch) >= args.wav2lip_batch_size:
            img_batch, mel_batch = np.asarray(img_batch), np.asarray(mel_batch)

            img_masked = img_batch.copy()
            img_masked[:, args.img_size // 2:] = 0

            img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
            mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])

            yield img_batch, mel_batch, frame_batch, coords_batch
            img_batch, mel_batch, frame_batch, coords_batch = [], [], [], []

    if len(img_batch) > 0:
        img_batch, mel_batch = np.asarray(img_batch), np.asarray(mel_batch)

        img_masked = img_batch.copy()
        img_masked[:, args.img_size // 2:] = 0

        img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
        mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])

        yield img_batch, mel_batch, frame_batch, coords_batch


mel_step_size = 16
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print('Using {} for inference.'.format(device))


def _load(checkpoint_path):
	if device == 'cuda':
		checkpoint = torch.load(checkpoint_path)
	else:
		checkpoint = torch.load(checkpoint_path,
								map_location=lambda storage, loc: storage)
	return checkpoint


def load_model(path):
	model = Wav2Lip()
	print("Load checkpoint from: {}".format(path))
	checkpoint = _load(path)
	s = checkpoint["state_dict"]
	new_s = {}
	for k, v in s.items():
		new_s[k.replace('module.', '')] = v
	model.load_state_dict(new_s)

	model = model.to(device)
	return model.eval()


##### For streaming #####
@app.route("/")
def index():
	# return the rendered template
	return render_template("index.html")

def generate():
	# grab global references to the output frame and lock variables
	global outputFrame, lock
	# loop over frames from the output stream
	while True:
		# wait until the lock is acquired
		with lock:
			# check if the output frame is available, otherwise skip
			# the iteration of the loop
			if outputFrame is None:
				continue
			# encode the frame in JPEG format
			(flag, encodedImage) = cv2.imencode(".jpg", outputFrame)
			# ensure the frame was successfully encoded
			if not flag:
				continue
		# yield the output frame in the byte format
		yield(b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' +
			bytearray(encodedImage) + b'\r\n')

@app.route("/video_feed")
def video_feed():
	# return the response generated along with the specific media
	# type (mime type)
	return Response(generate(),
		mimetype = "multipart/x-mixed-replace; boundary=frame")


def txt2vid_inference():
	global outputFrame, lock

	if not os.path.isfile(args.face):
		raise ValueError('--face argument must be a valid path to video/image file')

	elif args.face.split('.')[1] in ['jpg', 'png', 'jpeg']:
		full_frames = [cv2.imread(args.face)]
		fps = args.fps

	else:
		video_stream = cv2.VideoCapture(args.face)
		fps = video_stream.get(cv2.CAP_PROP_FPS)

		print('Reading video frames...')

		full_frames = []
		while 1:
			still_reading, frame = video_stream.read()
			if not still_reading:
				video_stream.release()
				break
			if args.resize_factor > 1:
				frame = cv2.resize(frame, (frame.shape[1] // args.resize_factor, frame.shape[0] // args.resize_factor))

			if args.rotate:
				frame = cv2.rotate(frame, cv2.cv2.ROTATE_90_CLOCKWISE)

			y1, y2, x1, x2 = args.crop
			if x2 == -1: x2 = frame.shape[1]
			if y2 == -1: y2 = frame.shape[0]

			frame = frame[y1:y2, x1:x2]

			full_frames.append(frame)

	print("Number of frames available for inference: " + str(len(full_frames)))

	# run face detection (precompute)
	face_det_results = face_detect_wrapper(full_frames)

	if not args.audio.endswith('.wav'):
		print('Extracting raw audio...')
		command = 'ffmpeg -y -i {} -strict -2 {}'.format(args.audio, 'temp/temp.wav')

		subprocess.call(command, shell=True)
		args.audio = 'temp/temp.wav'

	wav = audio.load_wav(args.audio, 16000)
	print('Len of input wav file:', len(wav))

	# Overall process works like this:
	# - split wav file into small chunks
	# - Initiate output stream for writing frames to intermediate video file
	# - Go through the audio chunks one by one. For each chunk:
	#     - compute melspectrrogram: mels
	#     - convert mel into overlapping chunks (#chunks = #frames correspoonding to audio chunk, e.g., for 200 ms audio and fps 25, we get 5 frames)
	#     - Now go through the mel_chunks and the input video frames, and run NN to compute the output frame one by one, which are written to the output stream
	# - Combine the output file with video with the original audio file to get final output

	# mel_idx_multiplier: this is supposed to align the audio melspec to the video fps,
	# by default set to 80.0/fps. This determines the mel chunking process, defining the
	# by which we move a window of size mel_step_size (16). For very short audio chunks, the
	# default vale doesn't work well due to rounding effects and edge effects leading to very
	# short mel vector relative to audio length. We fix this by reducing the mel_idx_multiplier
	# which reduces the offsets of the consecutive mel chunks, and makes sure we get enough
	# frames for each audio chunk.
	# NOTE: The value has been chosen for fps=25, and NUM_AUDIO_SAMPLES_PER_STEP 3200. For other values, please recalculate
	mel_idx_multiplier = 15.0 / fps

	# NUM_AUDIO_SAMPLES_PER_STEP: defines the chunks in which audio is processed.
	# Should be such that number of video frames within step is an integer
	# NOTE: Current system assumes 3200 (i.e., 200ms chunks)
	# NOTE: Can't make this much smaller, since that reduces the mel size to so small
	# that the mel_chunk produced is smaller than allowed by neural network architecture.
	NUM_AUDIO_SAMPLES_PER_STEP = 3200  # 200 ms for 16000 Hz

	num_audio_samples = len(wav)
	model = load_model(args.checkpoint_path)
	print("Model loaded")

	frame_h, frame_w = full_frames[0].shape[:-1]
	# # initiate video writer
	# out = cv2.VideoWriter('temp/result.avi',
	# 					  cv2.VideoWriter_fourcc(*'DIVX'), fps, (frame_w, frame_h))

	frames_done = 0
	for audio_step in tqdm(range(int(np.ceil(num_audio_samples // NUM_AUDIO_SAMPLES_PER_STEP)))):
		curr_wav = wav[audio_step * NUM_AUDIO_SAMPLES_PER_STEP:(audio_step + 1) * NUM_AUDIO_SAMPLES_PER_STEP]
		#       print(curr_wav.shape)
		#       print('start:',audio_step*NUM_AUDIO_SAMPLES_PER_STEP)
		#       print('end:',(audio_step+1)*NUM_AUDIO_SAMPLES_PER_STEP)
		mel = audio.melspectrogram(curr_wav)
		#        print(curr_wav)
		# print(mel.shape)

		if np.isnan(mel.reshape(-1)).sum() > 0:
			raise ValueError(
				'Mel contains nan! Using a TTS voice? Add a small epsilon noise to the wav file and try again')

		# mel_chunk generation process. Generate overlapping chunks, with the shift in
		# chunks determined by int(i * mel_idx_multiplier), and the chunk length is
		# mel_step_size = 16 (except for last chunk). Two important constraints to satisfy:
		# 1. len(mel_chunks) should be equal to number of frames to be generated according to
		#    fps and NUM_AUDIO_SAMPLES_PER_STEP
		# 2. Each mel_chunk must be sufficiently long otherwise NN gives error.
		mel_chunks = []

		i = 0
		while 1:
			start_idx = int(i * mel_idx_multiplier)
			if start_idx + mel_step_size > len(mel[0]):
				mel_chunks.append(mel[:, len(mel[0]) - mel_step_size:])
				break
			mel_chunks.append(mel[:, start_idx: start_idx + mel_step_size])
			i += 1

		# print("Length of mel chunks: {}".format(len(mel_chunks)))

		batch_size = args.wav2lip_batch_size
		gen = datagen(full_frames, face_det_results, mel_chunks, frames_done)

		for i, (img_batch, mel_batch, frames, coords) in enumerate(gen):

			img_batch = torch.FloatTensor(np.transpose(img_batch, (0, 3, 1, 2))).to(device)
			mel_batch = torch.FloatTensor(np.transpose(mel_batch, (0, 3, 1, 2))).to(device)

			with torch.no_grad():
				pred = model(mel_batch, img_batch)

			pred = pred.cpu().numpy().transpose(0, 2, 3, 1) * 255.

			for p, f, c in zip(pred, frames, coords):
				y1, y2, x1, x2 = c
				p = cv2.resize(p.astype(np.uint8), (x2 - x1, y2 - y1))

				f[y1:y2, x1:x2] = p
				#            print(f.dtype)
				#            cv2.imshow("mywindow",f)
				#            cv2.waitKey(1)
				# write generated frame to video writer (note: no audio right now)
				# out.write(f)
				with lock:
					outputFrame = f.copy()

				frames_done += 1
	# out.release()

	# # combine original audio and generated video
	# command = 'ffmpeg -y -i {} -i {} -strict -2 -q:v 1 {}'.format(args.audio, 'temp/result.avi', args.outfile)
	# subprocess.call(command, shell=platform.system() != 'Windows')

def main():
	# start a thread that will perform motion detection
	t = threading.Thread(target=txt2vid_inference)
	t.daemon = True
	t.start()

	# start the flask app
	app.run(host=args.ip, port=args.port, debug=True,
			threaded=True, use_reloader=False)

if __name__ == '__main__':
	main()
