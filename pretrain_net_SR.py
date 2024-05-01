import random
import torch
import torch.optim
import torch.nn.functional as F

import copy
import cv2
import time
import os
import glob
import numpy as np
import matplotlib.pyplot as plt
import utils.utils_image as util
import utils.utils_psf as util_psf
import utils.utils_train as util_train
from models.uabcnet import UABCNet as net

def main(dataset, kernel_path='./data', N_maxiter=10000, logs_directory="./logs", checkpoint_path=None):

	# ----------------------------------------
	# global configs
	# ----------------------------------------
	sf = 2					# HighRes patches are of shape (low_res_w*sf x low_res_h*sf)
	stage = 8
	patch_size = [64,64]	# LowRes patches are of shape (patch_size) 
	patch_num = [2,2]		# Takes 3x3=9 patches in one "batch"
	print("Global configs set.")
	
	# ----------------------------------------
	# load kernels
	# ----------------------------------------
	# Takes .npz files which can be generated by utils_psf.py
	# Normalizes them
	# Returns them in a list
	# Each psf is of the form (grid_height, grid_width, kernel_size, kernel_size, channel count)
	
	all_PSFs = util_psf.load_kernels(kernel_path)
	print("PSFs loaded.")


	# ----------------------------------------
	# define model
	# ----------------------------------------
	# Defines the architecture, the one used by Li et. al. can be described as follows
	# model = net(n_iter=8, h_nc=64, in_nc=4, out_nc=3, nc=[64, 128, 256, 512],
	# 				nb=2,sf=sf, act_mode="R", downsample_mode='strideconv', upsample_mode="convtranspose")
	# Head	x-x1		(conv from 4 to 64 channels)
	# Down1	x1-x2		(2*res 64 to 64 to 64 + down 64-128)
	# Down2	x2-x3		(2*res 128 to 128 to 128 + down 128 to 256)
	# Down3	x3-x4		(2*res 256 to 256 to 256 + down 256 to 512)
	# Body	x4-x		(2*res 512 to 512 to 512)
	# Up3	x+x4-x		(up 512 to 256 + 2* res 256 to 256 to 256)
	# Up2	x+x3-x		(up 256 to 128 + 2*res 128 to 128 to 128)
	# Up1	x+x2-x		(up 128 to 64 + 2*res 64 to 64 to 64)
	# Tail	x+x1-x		(conv from 64 to 3)

	device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
	model = net(n_iter=8, h_nc=64, in_nc=4, out_nc=3, nc=[64, 128, 256, 512],
					nb=2,sf=sf, act_mode="R", downsample_mode='strideconv', upsample_mode="convtranspose")
	if checkpoint_path is not None:
		model.load_state_dict(torch.load(checkpoint_path),strict=True)
	model.train()
		
	for _, v in model.named_parameters():
		v.requires_grad = True
	model = model.to(device)
	print("Model is loaded to:", device)

	# ----------------------------------------
	# positional lambda\mu for HQS
	# ----------------------------------------
	ab_buffer = np.ones((patch_num[0],patch_num[1],2*stage,3),dtype=np.float32)*0.1
	ab = torch.tensor(ab_buffer,device=device,requires_grad=True)
	print("Lambd/mu parameters for HQS set")


	# ----------------------------------------
	# build optimizer
	# ----------------------------------------
	params = []
	params += [{"params":[ab],"lr":0.0005}]
	for key,value in model.named_parameters():
		params += [{"params":[value],"lr":0.0001}]
	optimizer = torch.optim.Adam(params,lr=0.0001,betas=(0.9,0.999))
	scheduler = torch.optim.lr_scheduler.StepLR(optimizer,step_size=1000,gamma=0.9)
	print("Optimizer set")

	# ----------------------------------------
	# load training data
	# ----------------------------------------
	imgs_H = dataset
	random.shuffle(imgs_H)
	print("Training data loaded")

	# Take 10% images aside for validation
	imgs_val = imgs_H[:len(imgs_H)//10]
	imgs_H = imgs_H[len(imgs_H)//10:]
	print("Training on", len(imgs_H), "samples...")
	print("Validating on", len(imgs_val), "samples...")

	# This part can be uncommented to provide aberrated images directly instead of manual generation
	# In this case, the draw_training_pair() call within main() should also be modified accordingly
	# imgs_L = glob.glob('./images/DIV2K_lr/*.png', recursive=True)
	# imgs_L.sort()

	global_iter = 1
	losses = []
	val_ssims = []
	val_ssims_L = []
	val_psnrs = []
	val_psnrs_L = []

	best_model = copy.deepcopy(model.state_dict())
	best_ssim = 0

	for i in range(N_maxiter):
		#draw random image.
		img_idx = np.random.randint(len(imgs_H))
		img_H = cv2.imread(imgs_H[img_idx])
		# print("\nTraining on image:", img_idx)

		#draw random kernel
		PSF_grid = util_psf.draw_random_kernel(all_PSFs,patch_num)
		# print("Lens PSF choosen or generated:")

		# Cuts a random patch from the original image and the psf
		# Creates the noisy version
		patch_L,patch_H,patch_psf = util_train.draw_training_pair(img_H,PSF_grid,sf,patch_num,patch_size)
		# print("Patches are generated")

	
		x = util.uint2single(patch_L)
		x = util.single2tensor4(x)
		x_gt = util.uint2single(patch_H)
		x_gt = util.single2tensor4(x_gt)
	
		k_local = []
		for h_ in range(patch_num[1]):
			for w_ in range(patch_num[0]):
				k_local.append(util.single2tensor4(patch_psf[w_,h_]))
		k = torch.cat(k_local,dim=0)
		# print("Kernels are converted into tensors")

		# Data are moved to the gpu
		[x,x_gt,k] = [el.to(device) for el in [x,x_gt,k]]
		# print("Data loaded to:", device)
		
		ab_patch = F.softplus(ab)
		ab_patch_v = []
		for h_ in range(patch_num[1]):
			for w_ in range(patch_num[0]):
				ab_patch_v.append(ab_patch[w_:w_+1,h_])
		ab_patch_v = torch.cat(ab_patch_v,dim=0)

		# One forward pass is calculated
		x_E = model.forward_patchwise_SR(x,k,ab_patch_v,patch_num,[patch_size[0],patch_size[1]],sf)

		# Corresponding loss and gradiants are calculated
		# Weights are updated and the loss is logged
		loss = F.l1_loss(x_E,x_gt)
		losses.append(loss.item())
		optimizer.zero_grad()
		loss.backward()
		optimizer.step()
		scheduler.step()


		# Every 100 iterations, the image is saved and the model is validated
		# Every 2000 iterations, the model is saved
		if global_iter%100==0:
			patch_L = cv2.resize(patch_L,dsize=None,fx=sf,fy=sf,interpolation=cv2.INTER_NEAREST)
			patch_E = util.tensor2uint((x_E))
			util_train.save_triplet(f'{logs_directory}/images/pre{global_iter:05d}.png', patch_H, patch_L, patch_E)
			
			val_ssim, val_psnr, val_ssim_L, val_psnr_L = util_train.validate(imgs_val, PSF_grid, ab, sf, patch_num, patch_size, model, device)
			val_ssims.append(val_ssim)
			val_ssims_L.append(val_ssim_L)
			val_psnrs.append(val_psnr)
			val_psnrs_L.append(val_psnr_L)
			if val_ssim > best_ssim:
				best_ssim = val_ssim
				best_model = copy.deepcopy(model.state_dict())

		if global_iter%2000==0:
			torch.save(model.state_dict(),f"{logs_directory}/models/checkpoint_{global_iter:05d}.pth")
			print(f"Saved a model checkpoint at: {logs_directory}/models/checkpoint_{global_iter:05d}.pth")

		global_iter+= 1
	# End of Training


	torch.save(best_model, f"{logs_directory}/models/pretrained.pth")
	print(f"Saved the best model to {logs_directory}/models/pretrained.pth")

	print(f"Saving the training graphs to {logs_directory}/pretraining.png")
	_, (ax1, ax2, ax3) = plt.subplots(3)
	ax1.set(ylabel="training losses")
	ax1.plot(losses)
	
	ax2.plot(np.linspace(0, len(losses)-1,len(val_ssims)),val_ssims)
	ax2.plot(np.linspace(0, len(losses)-1,len(val_ssims)),val_ssims_L)
	ax2.set(ylabel="validation SSIMs")

	ax3.plot(np.linspace(0, len(losses)-1,len(val_psnrs)),val_psnrs)
	ax3.plot(np.linspace(0, len(losses)-1,len(val_psnrs)),val_psnrs_L)
	ax3.legend(["output", "input"], bbox_to_anchor=(1,0), fontsize="small")
	ax3.set(ylabel="validation PSNRs")
	
	plt.xlabel("iteration")
	plt.savefig(f"{logs_directory}/pretraining.png")
	# plt.show()

if __name__ == '__main__':
	print("Pretraining the model.")
	t0 = time.time()

	dataset = glob.glob('./images/DIV2K_train/*.png',recursive=True)
	dataset.extend(glob.glob('./images/cell_data/*.jpeg',recursive=True))

	main(
		dataset=dataset,
		kernel_path='./data',
		N_maxiter=10000,
		logs_directory="./logs")
	
	deltaT = time.time() - t0
	print(f"Pretraining completed in {deltaT/60:.2f} minutes")
