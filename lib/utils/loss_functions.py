import tensorflow as tf
import numpy as np
import os, sys
this_dir = os.path.abspath(os.path.dirname(__file__))
sys.path.append(this_dir)
from eval_functions import evaluation
sys.path.append(os.path.join(this_dir, '..', 'external_ops', 'evaluation_metric'))
from tf_approxmatch import *
from tf_nndistance import *

# ------ function ------
def int16Todepth(dMap, minVal=0, maxVal=10):
    dMap = (dMap/(pow(2,16)-1))*(maxVal-minVal) + minVal
    return dMap

def inverse_projection(Depth, K, Extrinsic, H=224, W=224):
    """
    Apply inverse-projection to the pre-rendered depth image
    Params:
    -- Depth     : Depth image. Tensor [batch_size, depth_H, depth_W]
    -- K         : Internal parameters. Tensor [batch_size, 3, 3]
    -- Extrinsic : External parameters. Tensor [batch_size, 4, 4]
    -- H         : Downsampled height
    -- W         : Downsampled weight

    Returns:
    -- Cloud     : Point cloud. Tensor [batch_size, point_number, 3]
    -- mask      : Foreground point mask. Tensor [batch_size, point_number]
    """

    downscale_H = 224.0/H
    downscale_W = 224.0/W
    Depth = tf.to_float(Depth)
    if downscale_H >1 or downscale_W > 1:
        Depth = -(tf.nn.max_pool(-tf.expand_dims(Depth, axis=3), ksize=[1,int(downscale_H),int(downscale_W),1],strides=[1,int(downscale_H),int(downscale_W),1],padding="VALID"))
        Depth = tf.squeeze(Depth)
    K = K * np.array([[1.0/downscale_H], [1.0/downscale_W], [1]], dtype=np.float32)
    batchSize = tf.shape(Depth)[0]
    Depth = int16Todepth(Depth) #[B,H,W]
    mask = (Depth < 10)
    mask = tf.reshape(mask, [batchSize, H*W]) #[B,H*W]

    K_inverse = tf.matrix_inverse(K) #[B,3,3]
    Extrinsic_inverse = tf.matrix_inverse(Extrinsic) #[B,4,4]
    index = np.zeros([H, W, 2])
    for i in range(H):
        for j in range(W):
            index[i][j][0] = j
            index[i][j][1] = i
    index = tf.convert_to_tensor(index, dtype=tf.float32) #[H,W,2]
    index = tf.tile(tf.expand_dims(index,axis=0),[batchSize,1,1,1]) #[B,H,W,2]
    Depth =  tf.to_float(tf.expand_dims(Depth, axis=3)) #[B,H,W,1]
    index = tf.multiply(index, tf.tile(Depth,[1,1,1,2]))
    Cloud_pre = tf.concat([index, Depth], axis=3) #[B,H,W,3]
    Cloud_pre = tf.reshape(Cloud_pre, [batchSize, H*W, 3]) #[B,H*W,3]
    Cloud_pre = tf.transpose(Cloud_pre, [0,2,1]) #[B,3,H*W]
    Cloud = tf.matmul(K_inverse, Cloud_pre) #[B,3,H*W]
    ones = tf.ones([batchSize,1,H*W])
    Cloud = tf.concat([Cloud,ones], axis=1) #[B,4,H*W]
    Cloud = tf.matmul(Extrinsic_inverse, Cloud) #[B,4,H*W]
    Cloud = Cloud[:,:3,:] #[B,3,H*W]
    Cloud = tf.transpose(Cloud, [0,2,1]) #[B,H*W,3]
    return Cloud, mask

def depthToint16(dMap, minVal=0, maxVal=10):
    dMap = tf.where(dMap<maxVal, dMap, tf.ones_like(dMap)*maxVal)
    dMap = ((dMap-minVal)*(pow(2,16)-1)/(maxVal-minVal))
    return dMap

def projection(XYZ, K, Extrinsic, H=224, W=224, reuse=False): #[B,N,3],[B,3,3],[B,4,4]
    """
    Apply projection to the point cloud
    Params:
    -- XYZ       : Point cloud. Tensor [batch_size, point_number, 3
    -- K         : Internal parameters. Tensor [batch_size, 3, 3]
    -- Extrinsic : External parameters. Tensor [batch_size, 4, 4]
    -- H         : Downsampled height
    -- W         : Downsampled weight

    Returns:
    -- newDepth  : Depth image. Tensor [batch_size, H, W]
    -- Cloud_mask: Front(visible) points mask. Tensor[batch_size, point_number]
    """


    XYZ = tf.transpose(XYZ, [0,2,1])
    batchSize = tf.shape(XYZ)[0]
    downscale_H = 224.0/H
    downscale_W = 224.0/W
    K = K*np.array([[1.0/downscale_H], [1.0/downscale_W], [1]], dtype=np.float32)
    N = tf.shape(XYZ)[2]
    H = tf.constant(H)
    W = tf.constant(W)
    bg = pow(2,16) - 1
    with tf.variable_scope("transform_render2D") as scope:
        if reuse:
            scope.reuse_variables()
        
        # ------ use camera calibration to compute new XYZ ------
        ones = tf.ones([batchSize, 1, N])
        XYZ = tf.concat([XYZ,ones], axis=1)# [B,4,N]
        XYZtemp = tf.matmul(Extrinsic, XYZ)# [B,4,N] = [B,4,4]*[B,4,N]
        XYZtemp = XYZtemp[:,:3,:]
        XYZnew = tf.matmul(K, XYZtemp)# [B,3,N] = [B,3,3]*[B,3,N]
        XYZnew = tf.transpose(XYZnew, [0,2,1]) # [B,N,3]
        eps = 1e-12
        X = tf.reshape(tf.to_int32(tf.round(tf.div(XYZnew[:,:,0], XYZnew[:,:,2] + eps))), [-1]) #[B*N,]
        Y = tf.reshape(tf.to_int32(tf.round(tf.div(XYZnew[:,:,1], XYZnew[:,:,2] + eps))), [-1]) #[B*N,]
        YX = tf.stack([Y,X], axis=1) #[B*N,2]
        Batch = tf.range(0, batchSize, 1)
        Batch = tf.tile(tf.expand_dims(Batch, axis=1),[1,N]) 
        Batch = tf.reshape(Batch, [batchSize*N, 1])
        scatterIndex = tf.concat([Batch, YX], axis=1) #[B*N,3]         
        scatterZ = tf.reshape(XYZnew[:,:,2],[-1]) #[B*N,]
        
        # ------ delete invalid points ------
        _, Y_Index, X_Index = tf.split(scatterIndex, 3, axis=1) #[B*N,1]
        X_Index = tf.squeeze(X_Index)
        Y_Index = tf.squeeze(Y_Index)
        Cloud_mask_pre = tf.range(0,batchSize*N,1)
        mask_inside = (X_Index >= 0)&(X_Index < W)&(Y_Index >= 0)&(Y_Index < H)&(scatterZ >=0)&(scatterZ <=10)
        mask_inside.set_shape([None])
        Cloud_mask_pre = tf.boolean_mask(Cloud_mask_pre,mask_inside)
        scatterIndex = tf.boolean_mask(scatterIndex, mask_inside)
        scatterZ = depthToint16(tf.boolean_mask(scatterZ, mask_inside)) #[B*N,]

        # ------ select front (visible) points ------
        seg_id = scatterIndex[:,0]*H*W + scatterIndex[:,1]*W + scatterIndex[:,2]
        seg_min = tf.unsorted_segment_max(-scatterZ, seg_id, batchSize*H*W) #[B*H*W,]
        seg_mask = tf.gather_nd(-seg_min, tf.expand_dims(seg_id, axis=1)) #[B*N,]
        mask = ((scatterZ - seg_mask) <= 0)
        Cloud_mask_pre = tf.boolean_mask(Cloud_mask_pre, mask)
        scatterIndex = tf.boolean_mask(scatterIndex, mask)

        # ------ compute depth images ------
        scatterZ = tf.boolean_mask(scatterZ, mask)
        scatterZ =  scatterZ - bg
        newDepth = tf.scatter_nd(scatterIndex, scatterZ, shape=[batchSize, H, W]) #[B,H,W]
        newDepth = newDepth + bg
  
        # ------ compute front mask given extrinsic ------
        Cloud_mask = tf.scatter_nd(tf.expand_dims(Cloud_mask_pre, axis=1), tf.ones_like(Cloud_mask_pre), shape=[batchSize*N])
        Cloud_mask = (Cloud_mask > 0)
        Cloud_mask = tf.reshape(Cloud_mask, [batchSize,N])
   
        return newDepth, Cloud_mask

def front_loss(XYZ, K, Extrinsic, DepthGT, H=224, W=224, is_emd=True, reuse=False):
    '''
    Front Loss. 
    Params:
    -- XYZ : Prediction point cloud. Tensor [batch_size, point_number, 3]
    -- K         : Internal parameters. Tensor [batch_size, 3, 3]
    -- Extrinsic : External parameters. Tensor [batch_size, 4, 4]
    -- DepthGT   : Groundtruth depth images. Tensor [batch_size, depth_H, depth_W]
    -- H         : Downsampled height
    -- W         : Downsampled weight
    -- is_emd    : Whether to use EMD.

    Returns:
    -- loss
    '''
    XYZ_inverse, mask_inverse = inverse_projection(DepthGT, K, Extrinsic, H=H, W=W)
    _, mask = projection(XYZ, K, Extrinsic, H=H, W=W, reuse=False)
    batchSize = tf.shape(XYZ)[0]
    i = tf.constant(0)
    loss = tf.constant(0.0, dtype=tf.float32)
    def cond_loss(i, loss):
        return tf.less(i, batchSize)

    def body_loss(i, loss):
        XYZ_temp = tf.boolean_mask(XYZ[i,:,:], mask[i,:])
        XYZ_inverse_temp = tf.boolean_mask(XYZ_inverse[i,:,:], mask_inverse[i,:])
        XYZ_temp = tf.expand_dims(XYZ_temp, axis=0)
        XYZ_inverse_temp = tf.expand_dims(XYZ_inverse_temp, axis=0)
        if is_emd :
           loss_temp = evaluation(XYZ_inverse_temp, XYZ_temp, is_emd)
        else:
           loss_temp, _, _ = evaluation(XYZ_inverse_temp, XYZ_temp, is_emd)
        return i+1, loss+loss_temp
    i, loss = tf.while_loop(cond_loss, body_loss, [i, loss])
    return tf.div(loss, tf.to_float(batchSize))



def diversity_loss(data1, data2, alpha, is_emd):
    '''
    Diversity Loss. 
    Params:
    -- data1  : (r1, pc1). Tensor [(batch_size, dim), (batch_size, point_num, 3)]
    -- data2  : (r2, pc2). Tensor [(batch_size, dim), (batch_size, point_num, 3)]

    Returns:
    -- loss   
    '''
    r1, pc1 = data1
    r2, pc2 = data2
    dist_r = tf.norm(r1 - r2, axis=1)
    if is_emd == True:
        dist_pc = approx_match(pc1, pc2)
    else:
        dist1, dist2 = nn_distance(pc1, pc2) #dist1: gt->pred, dist2: pred->gt
        dist_pc = 100*(dist1 + dist2)
    loss = tf.maximum(dist_r - dist_pc * alpha, 0)
    loss = tf.reduce_mean(loss, axis=0)
    return loss

def get_loss_diversity(r, pc, rand_num, alpha=0.2, is_emd=True):
    '''
    Compute diversity loss for a batch
    Params:
    -- r : Random vector. Tensor [rand_num, batch_size, rand_dim]
    -- pc : Point cloud. Tensor [rand_num, batch_size, point_number, 3]
    -- rand_num : The number of random inputs per image
    -- alpha : Hyper-parameter of diversity loss
    -- is_emd : Whether to use EMD.

    Returns:
    -- loss
    '''
    r_ = [r[0] for r in tf.split(r, rand_num)]
    pc_ = [pc[0] for pc in tf.split(pc, rand_num)]
    data = zip(r_, pc_)
    loss = tf.constant(0.0)
    for i in range(rand_num):
        for j in range(rand_num):
            if i >= j:
                continue
            loss += diversity_loss(data[i], data[j], alpha=alpha, is_emd=is_emd) 
    loss = loss/(rand_num*(rand_num-1)/2)
    return loss

def consis_loss(Cloud, Extrinsic, num_camera, is_emd=False, batch_once=100):  #[B,C,N,3],[B,C,4,4]
    '''
    Consistence Loss
    Params:
    -- Cloud      : Point cloud. Tensor [batchsize, num_camera, num_points, 3]
    -- Extrinsic  : External parameters. Tensor [batchsize, num_camera, 4, 4]
    -- num_camera : The number of views
    -- is_emd     : Whether use EMD as evaluation metric

    Returns:
    -- loss: Tensor [batchsize*num_camera*(num_camera-1)/2,]
    '''
    batchsize = tf.shape(Cloud)[0]
    cam_index1 = np.zeros(num_camera*(num_camera-1)/2)
    cam_index2 = np.zeros(num_camera*(num_camera-1)/2)
    count = -1
    # ------ produce index ------
    for i in range(num_camera): 
        for j in range(num_camera):
            if i >= j:
                continue
            count += 1
            cam_index1[count] = j
            cam_index2[count] = i
    cam_index1 = tf.to_int32(tf.convert_to_tensor(cam_index1))
    cam_index2 = tf.to_int32(tf.convert_to_tensor(cam_index2))
    cam_index1 = tf.tile(cam_index1,[batchsize])
    cam_index2 = tf.tile(cam_index2,[batchsize])
    batch_index = tf.range(0,batchsize,1)
    batch_index = tf.expand_dims(batch_index, axis=1)
    batch_index = tf.tile(batch_index, [1,num_camera*(num_camera-1)/2])
    batch_index = tf.reshape(batch_index,[-1,1])
    index1 = tf.concat([batch_index,tf.expand_dims(cam_index1,axis=1)],axis=1)
    index2 = tf.concat([batch_index,tf.expand_dims(cam_index2,axis=1)],axis=1)
  
    # ------ coordinate system conversion ------
    Extrinsic_des = tf.gather_nd(Extrinsic, index1)
    Extrinsic_src = tf.gather_nd(Extrinsic, index2)
    Extrinsic_src_inverse = tf.matrix_inverse(Extrinsic_src)
    Extrinsic = tf.matmul(Extrinsic_des, Extrinsic_src_inverse)        

    Cloud_des = tf.gather_nd(Cloud, index1) #[B*C*(C-1), N, 3]
    Cloud_src = tf.gather_nd(Cloud, index2)
    Cloud_src = tf.transpose(Cloud_src,(0,2,1))
    ones = tf.ones_like(Cloud_src)
    ones = ones[:,0:1,:]
    Cloud_src = tf.concat([Cloud_src,ones], axis=1)
    Cloud_src = tf.matmul(Extrinsic, Cloud_src)
    Cloud_src = tf.transpose(Cloud_src[:,:3,:],(0,2,1)) #[B*C*(C-1), N, 3]

    # ------ split batch (avoid OOM) ------
    if is_emd == True:
       batch_once = batch_once/2
    batch_num = tf.cast(tf.ceil(tf.div(tf.cast(tf.shape(Cloud_src)[0], tf.float32), np.float(batch_once))),tf.int32)
    def body(i, loss):
        if is_emd == True:
            loss_temp = approx_match(Cloud_des[i*batch_once:(i+1)*batch_once,:], Cloud_src[i*batch_once:(i+1)*batch_once,:])
        else:
            dist1, dist2 = nn_distance(Cloud_des[i*batch_once:(i+1)*batch_once,:], Cloud_src[i*batch_once:(i+1)*batch_once,:])
            loss_temp = 100*(dist1 + dist2)
        loss = tf.concat([loss,loss_temp], axis=0)
        return i+1, loss
    def cond(i, loss):  
        return tf.less(i, batch_num)
    i = tf.constant(0)
    loss = tf.constant([0.0])
    i, loss = tf.while_loop(cond, body, [i, loss], shape_invariants = [i.get_shape(), tf.TensorShape([None])])
    return loss[1:]

def FGSM(inputs, y, eps=0.0, clip_min=0.,clip_max=1.):
    x = inputs
    dy_dx = tf.gradients(y, x)
    x = x - eps*tf.to_float(dy_dx)
    return x, y 


