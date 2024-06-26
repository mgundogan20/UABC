import torch
import torch.optim
import torch.nn.functional as F
import numpy as np

import cv2
from PIL import Image, ImageDraw
import utils.utils_image as util
import utils.utils_deblur as util_deblur


def draw_training_pair(image_H,psf,ab,sf,patch_num,patch_size,image_L=None):
	# image_H is the ground truth
	# psf is a grid of PSFs (GH x GW x H x W x C)
	# image_L is the aberrated image. When not provided, it's generated by the function
	# Returns patch_H, patch_L, patch_PSF

	w,h = image_H.shape[:2]
	gx,gy = psf.shape[:2]

	max_X = gx-patch_num[0]
	max_Y = gy-patch_num[1]

	if max_X == 0:
		px_start = 0
	else:
		prob_X = np.array([(i-max_X/2)**4 for i in range(max_X+1)])
		prob_X /= prob_X.sum()
		px_start = np.random.choice(range(max_X+1), p=prob_X)

	if max_Y == 0:
		py_start = 0
	else:
		prob_Y = np.array([(i-max_Y/2)**4 for i in range(max_Y+1)])
		prob_Y /= prob_Y.sum()
		py_start = np.random.choice(range(max_Y+1), p=prob_Y)

	psf_patch = psf[px_start:px_start+patch_num[0],py_start:py_start+patch_num[1]]
	ab_patch = ab[px_start:px_start+patch_num[0],py_start:py_start+patch_num[1]]

	patch_size_H = [patch_size[0]*sf,patch_size[1]*sf]

	if image_L is None:
		#generate image_L on-the-fly
		conv_expand = psf.shape[2]//2
		x_start = np.random.randint(0,w-patch_size_H[0]*patch_num[0]-conv_expand*2+1)
		y_start = np.random.randint(0,h-patch_size_H[1]*patch_num[1]-conv_expand*2+1)
		patch_H = image_H[x_start:x_start+patch_size_H[0]*patch_num[0]+conv_expand*2,\
		y_start:y_start+patch_size_H[1]*patch_num[1]+conv_expand*2]
		patch_L = util_deblur.blockConv2d(patch_H,psf_patch,conv_expand)

		patch_H = patch_H[conv_expand:-conv_expand,conv_expand:-conv_expand]
		patch_L = patch_L[::sf,::sf]

		#wrap_edges around patch_L to avoid FFT boundary effect.
		#wrap_expand = patch_size[0]//8
		# patch_L_wrap = util_deblur.wrap_boundary_liu(patch_L,(patch_size[0]*patch_num[0]+wrap_expand*2,\
		# patch_size[1]*patch_num[1]+wrap_expand*2))
		# patch_L_wrap = np.hstack((patch_L_wrap[:,-wrap_expand:,:],patch_L_wrap[:,:patch_size[1]*patch_num[1]+wrap_expand,:]))
		# patch_L_wrap = np.vstack((patch_L_wrap[-wrap_expand:,:,:],patch_L_wrap[:patch_size[0]*patch_num[0]+wrap_expand,:,:]))
		# patch_L = patch_L_wrap

	else:
		x_start = px_start * patch_size_H[0]
		y_start = py_start * patch_size_H[1]
		x_end = x_start + (patch_size_H[0]*patch_num[0])
		y_end = y_start + (patch_size_H[1]*patch_num[1])
		patch_H = image_H[x_start:x_end, y_start:y_end]

		x_start = px_start * patch_size[0]
		y_start = py_start * patch_size[1]
		x_end = x_start + (patch_size[0]*patch_num[0])
		y_end = y_start + (patch_size[1]*patch_num[1])
		patch_L = image_L[x_start:x_end, y_start:y_end]

	return patch_L,patch_H,psf_patch,ab_patch


def validate(imgs_val, PSF_grid, ab, sf, patch_num, patch_size, model, device, imgs_val_L=None):
	model.eval()
	ssim = 0
	ssim_L = 0
	psnr = 0
	psnr_L = 0

	with torch.no_grad():
		for img_idx in range(len(imgs_val)):
			img_H = cv2.imread(imgs_val[img_idx])

			if imgs_val_L is None:
				patch_L,patch_H,patch_psf,patch_ab = draw_training_pair(img_H,PSF_grid,ab,sf,patch_num,patch_size)
			else:
				img_L = cv2.imread(imgs_val_L[imgs_val[img_idx]])
				patch_L,patch_H,patch_psf,patch_ab = draw_training_pair(img_H,PSF_grid,ab,sf,patch_num,patch_size,image_L=img_L)

			x = util.uint2single(patch_L)
			x = util.single2tensor4(x)
			x_gt = util.uint2single(patch_H)
			x_gt = util.single2tensor4(x_gt)

			k_local = []
			for h_ in range(patch_num[1]):
				for w_ in range(patch_num[0]):
					k_local.append(util.single2tensor4(patch_psf[w_,h_]))
			k = torch.cat(k_local,dim=0)
			[x,x_gt,k] = [el.to(device) for el in [x,x_gt,k]]
			
			ab_patch = F.softplus(ab)
			ab_patch_v = []
			for h_ in range(patch_num[1]):
				for w_ in range(patch_num[0]):
					ab_patch_v.append(ab_patch[w_:w_+1,h_])
			ab_patch_v = torch.cat(ab_patch_v,dim=0)
	
			x_E = model.forward_patchwise_SR(x,k,ab_patch_v,patch_num,[patch_size[0],patch_size[1]],sf)
			patch_E = util.tensor2uint((x_E))
			
			ssim += util.calculate_ssim(patch_H, patch_E)
			psnr += util.calculate_psnr(patch_H, patch_E)
			
			ssim_L += util.calculate_ssim(patch_H, patch_L)
			psnr_L += util.calculate_psnr(patch_H, patch_H)

	model.train()
	return (ssim/len(imgs_val), psnr/len(imgs_val), ssim_L/len(imgs_val), psnr_L/len(imgs_val))

def save_triplet(path, patch_H, patch_L, patch_E):

	ssim_E = util.calculate_ssim(patch_H,patch_E)
	ssim_L = util.calculate_ssim(patch_H,patch_L)
	psnr_E = util.calculate_psnr(patch_H,patch_E)
	psnr_L = util.calculate_psnr(patch_H,patch_L)

	show = Image.fromarray(np.hstack((patch_H,patch_L,patch_E)))

	draw = ImageDraw.Draw(show)

	text_color = (255, 255, 255)
	text_outline_color = (0, 0, 0)
	text_outline_thickness = 1

	draw.text((10+2*patch_H.shape[1],10), f"SSIM_E: {ssim_E:.4f}", fill=text_color, stroke_fill=text_outline_color, stroke_width=text_outline_thickness)
	draw.text((10+2*patch_H.shape[1],30), f"PSNR_E: {psnr_E:.4f}", fill=text_color, stroke_fill=text_outline_color, stroke_width=text_outline_thickness)
	draw.text((10+patch_H.shape[1],10), f"SSIM_L: {ssim_L:.4f}", fill=text_color, stroke_fill=text_outline_color, stroke_width=text_outline_thickness)
	draw.text((10+patch_H.shape[1],30), f"PSNR_L: {psnr_L:.4f}", fill=text_color, stroke_fill=text_outline_color, stroke_width=text_outline_thickness)
	
	show = show.convert('RGB')
	show = np.array(show)

	# cv2.imshow('H,L,E',np.array(show))
	# cv2.waitKey(1)

	cv2.imwrite(path,show)