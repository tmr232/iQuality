# Copyright (C) 2012-2013 Itay Brandes

'''
A Rewritten version of SmartDownload as a class.

Usage:
>>> obj = SmartDL(url, dest=None, max_threads=5,show_output=False ,logger=None)
>>> obj.start(blocking=False)
>>> print "Eta is %d" % obj.get_eta()
>>> print "speed is %d" % obj.get_speed()
>>> print "downloaded size is %d" % obj.get_downloaded_size()
>>> print "dest path is %d" % obj.get_dest()
>>> print "isFinished: %s" % str(obj.isFinished())
>>> print "status: %s" % obj.status # may be one of the following: ready, downloading, paused, combining, finished
>>> obj.wait()
>>> os.startfile(obj.get_dest())

:param url: Download url.
:param dest: Destination path. Default is in temp folders.
:param max_threads: Maximum amount of threads. Default is 5.
:param show_output: If True, prints a progress bar to the screen. Default is False.
:param logger: An optional logger.

About the shared object counting the download status, I've checked the performance for some different objects?

z: global var
shared1: SharedObject() (object with thread locks)
shared2: multiprocessing.Value

Here are the counters, where the real filesize is 67060045:
z: 67027277, shared1: 64495949, shared2: 67010893
z: 67060045, shared1: 66265421, shared2: 67043661
z: 67060045, shared1: 65364301, shared2: 67060045
z: 67051853, shared1: 65118541, shared2: 67043661
z: 67060045, shared1: 65927843, shared2: 67060045
z: 67051853, shared1: 63584931, shared2: 67043661
z: 67035469, shared1: 64569677, shared2: 67051853

It seems that shared1 is highly incorrect (3.72% approx. error).
z has error rate of 0.07%.
shared2 has error rate of 0.06%.

It is not clear why shared1 fails, but we'll stick with shared2 concept, because of very low error rate.

'''

import os
import urllib2
import copy
import logging
import threading
import time
from urlparse import urlparse
import multiprocessing.dummy as multiprocessing
from ctypes import c_int

from threadpool import ThreadPool
import Config; config = Config.config
from logger import log
import utils
from HTTPQuery import is_ServerSupportHTTPRange

class SmartDL:
	"The main SmartDL class."
	def __init__(self, url, dest=None, max_threads=5, show_output=True, logger=None):
		self.url = url if not " " in url else utils.url_fix(url)
		self.dest = dest or r"%s\%s" % (config.temp_dir, urlparse(self.url).path.split('/')[-1])
		self.show_output = show_output
		self.logger = logger or logging.getLogger('dummy')
		self.max_threads = max_threads
		
		self.headers = config.generic_http_headers
		self.timeout = 4
		self.minChunkFile = 1024**2 # 1MB
		self.filesize = 0
		self.shared_var = multiprocessing.Value(c_int, 0) # a ctypes var that counts the bytes already downloaded
		self.status = "ready"
		
		self.post_threadpool_thread = None
		self.control_thread = None
		
		if not is_ServerSupportHTTPRange(self.url):
			self.logger.warning("Server does not support HTTPRange. max_threads is set to 1.")
			self.max_threads = 1
		if os.path.exists(self.dest):
			self.logger.warning("Destination '%s' already exists. Existing file will be removed." % self.dest)
		if not os.path.exists(os.path.dirname(self.dest)):
			self.logger.warning("Directory '%s' does not exist. Creating it..." % os.path.dirname(self.dest))
			os.makedirs(os.path.dirname(self.dest))
			
		self.pool = ThreadPool(max_threads=self.max_threads, catch_returns=True, logger=self.logger)
		
	def __str__(self):
		return 'SmartDL(url=r"%s", dest=r"%s", show_output=%s)' % (self.url, self.dest, self.show_output)
	def __repr__(self):
		return "<SmartDL %s>" % (self.url)
		
	def start(self, blocking=True):
		"Starts the download task"
		if not self.status == "ready":
			raise RuntimeError("cannot start (current status is %s)" % self.status)
		self.logger.debug("Downloading '%s' to '%s'..." % (self.url, self.dest))
		req = urllib2.Request(self.url, headers=self.headers)
		urlObj = urllib2.urlopen(req, timeout=self.timeout)
		meta = urlObj.info()
		try:
			self.filesize = int(meta.getheaders("Content-Length")[0])
			self.logger.debug("Content-Length is %d (%.2fMB)." % (self.filesize, self.filesize/1024.0**2))
		except IndexError:
			self.logger.warning("Server did not send Content-Length.")
			self.filesize = 0
			
		args = calc_args(self.filesize, self.max_threads, self.minChunkFile)
		bytes_per_thread = args[0][1]-args[0][0]
		if len(args)>1:
			self.logger.debug("Launching %d threads (downloads %dKB in each thread)." % (len(args), bytes_per_thread/1024))
		else:
			self.logger.debug("Launching 1 thread.")
		
		self.status = "downloading"
		for i, arg in enumerate(args):
			x = [self.url, self.dest+".%.3d" % i, arg[0],
					arg[1], copy.deepcopy(self.headers), self.timeout, self.shared_var]
			self.pool(download)(*x)
		
		self.post_threadpool_thread = threading.Thread(target=post_threadpool_actions, args=(self.pool, [[(self.dest+".%.3d" % i) for i in range(len(args))], self.dest]))
		self.post_threadpool_thread.daemon = True
		self.post_threadpool_thread.start()
		
		self.control_thread = ControlThread(self)
	
	def get_eta(self):
		return self.control_thread.get_eta()
	def get_speed(self):
		return self.control_thread.get_speed()
	def get_downloaded_size(self):
		return self.control_thread.get_downloaded_size()
	def get_dest(self):
		return self.dest
	def get_progress(self):
		if not self.filesize:
			return 0
		return 1.0*self.control_thread.get_downloaded_size()/self.filesize
	def isFinished(self):
		if self.status == "ready":
			return False
		if self.status == "finished":
			return True
		return not self.post_threadpool_thread.is_alive()
	def get_dl_time(self):
		return self.control_thread.get_dl_time()
	def wait(self):
		if self.status == "finished":
			return
			
		while not self.isFinished():
			time.sleep(0.1)
		self.post_threadpool_thread.join()
		self.control_thread.join()
	def pause(self):
		if self.status == "downloading":
			self.pool.pause()
	def unpause(self):
		if self.status == "downloading":
			self.pool.unpause()
	def stop(self):
		if self.status == "downloading":
			self.pool.terminate_nowait()

class ControlThread(threading.Thread):
	"A class that shows information about a running SmartDL object."
	def __init__(self, obj):
		threading.Thread.__init__(self)
		self.obj = obj
		self.show_output = obj.show_output
		self.logger = obj.logger
		self.shared_var = obj.shared_var
		
		self.dl_speed = 0
		self.eta = 0
		self.lastBytesSamples = [] # list with last 50 Bytes Samples.
		self.last_calculated_totalBytes = 0
		self.calcETA_queue = []
		self.calcETA_i = 0
		self.calcETA_val = 0
		self.dl_time = -1.00
		
		self.daemon = True
		self.start()
		
	def run(self):
		t1 = time.time()
		
		while not self.obj.pool.isFinished():
			self.dl_speed = self.calcDownloadSpeed(self.shared_var.value)
			if self.dl_speed > 0:
				self.eta = self.calcETA((self.obj.filesize-self.shared_var.value)/self.dl_speed)
				
			if self.show_output:
				if self.obj.filesize:
					status = r"%.2f / %.2f MB @ %.2fKB/s %s [%3.2f%%, %ds left]    " % (self.shared_var.value / 1024.0**2, self.obj.filesize / 1024.0**2, self.dl_speed/1024.0, utils.progress_bar(1.0*self.shared_var.value/self.obj.filesize), self.shared_var.value * 100.0 / self.obj.filesize, self.eta)
				else:
					status = r"%.2f / ??? MB @ %.2fKB/s" % (self.shared_var.value / 1024.0**2, self.dl_speed/1024.0)
				status = status + chr(8)*(len(status)+1)
				print status,
			time.sleep(0.1)
			
		if self.show_output:
			if self.obj.filesize:
				print r"%.2f / %.2f MB @ %.2fKB/s %s [100%%, 0s left]    " % (self.obj.filesize / 1024.0**2, self.obj.filesize / 1024.0**2, self.dl_speed/1024.0, utils.progress_bar(1.0))
			else:
				print r"%.2f / %.2f MB @ %.2fKB/s" % (self.shared_var.value / 1024.0**2, self.shared_var.value / 1024.0**2, self.dl_speed/1024.0)
				
		t2 = time.time()
		self.dl_time = float(t2-t1)
		
		self.logger.debug("Combining files...") # actually happens on post_threadpool_thread
		self.obj.status = "combining"
		while self.obj.post_threadpool_thread.is_alive():
			time.sleep(0.1)
		
		self.obj.status = "finished"
		self.logger.debug("File downloaded within %.2f seconds." % self.dl_time)
			
	def get_eta(self):
		return self.eta
	def get_speed(self):
		return self.dl_speed
	def get_downloaded_size(self):
		return self.shared_var.value
	def get_dl_time(self):
		return self.dl_time
		
	def calcDownloadSpeed(self, totalBytes, sampleCount=30, sampleDuration=0.1):
		'''
		Function calculates the download rate.
		@param totalBytes: The total amount of bytes.
		@param sampleCount: How much samples should the function take into consideration.
		@param sampleDuration: Duration of a sample in seconds.
		'''
		l = self.lastBytesSamples
		newBytes = totalBytes - self.last_calculated_totalBytes
		self.last_calculated_totalBytes = totalBytes
		if newBytes >= 0: # newBytes may be negetive, will happen
						  # if a thread has crushed and the totalBytes counter got decreased.
			if len(l) == sampleCount: # calc download for last 3 seconds (30 * 100ms per signal emit)
				l.pop(0)
				
			l.append(newBytes)
			
		dlRate = sum(l)/len(l)/sampleDuration
		return dlRate
		
	def calcETA(self, eta):
		self.calcETA_i += 1
		l = self.calcETA_queue
		l.append(eta)
		
		if self.calcETA_i % 10 == 0:
			self.calcETA_val = sum(l)/len(l)
		if len(l) == 30:
			l.pop(0)

		if self.calcETA_i < 50:
			return 0
		return self.calcETA_val

def post_threadpool_actions(pool, args):
	"Run function after thread pool is done. Run this in a thread."
	while not pool.isFinished():
		time.sleep(0.1)
	combine_files(*args)
	
def calc_args(filesize, max_threads, minChunkFile):
	if not filesize:
		return [(0, 0)]
		
	threads = max_threads
	while filesize/threads < minChunkFile and threads > 1:
		threads -= 1
		
	args = []
	pos = 0
	chunk = filesize/threads
	for i in range(threads):
		startByte = pos
		endByte = pos + chunk
		if endByte > filesize-1:
			endByte = filesize-1
		args.append((startByte, endByte))
		pos += chunk+1
		
	return args

@utils.decorators.retry(Exception, logger=logging.getLogger('testingLog'))
def download(url, dest, startByte=0, endByte=None, headers=None, timeout=4, shared_var=None, logger=None):
	logger = logger or logging.getLogger('dummy')
	if not headers:
		headers = {}
	if endByte:
		headers['Range'] = 'bytes=%d-%d' % (startByte, endByte)
		
	logger.debug("Downloading '%s' to '%s'..." % (url, dest))
	req = urllib2.Request(url, headers=headers)
	try:
		urlObj = urllib2.urlopen(req, timeout=timeout)
	except urllib2.HTTPError, e:
		if "HTTP Error 416" in str(e):
			# HTTP 416 Error: Requested Range Not Satisfiable. Happens when we ask
			# for a range that is not available on the server. It will happen when
			# the server will try to send us a .html page that means something like
			# "you opened too many connections to our server". If this happens, we
			# will wait for the other threads to finish their connections and try again.
			
			logger.warning("Thread didn't got the file it was expecting. Retrying...")
			time.sleep(5)
			download(url, dest, startByte, endByte, headers, timeout, shared_var, logger)
		else:
			raise
	
	with open(dest, 'wb') as f:
		if endByte:
			filesize = endByte-startByte
		else:
			try:
				meta = urlObj.info()
				filesize = int(meta.getheaders("Content-Length")[0])
				logger.debug("Content-Length is %d." % filesize)
			except IndexError:
				logger.warning("Server did not send Content-Length.")
		
		filesize_dl = 0
		block_sz = 8192
		while True:
			try:
				buff = urlObj.read(block_sz)
			except Exception:
				if shared_var:
					shared_var.value -= filesize_dl
				raise
				
			if not buff:
				break

			filesize_dl += len(buff)
			if shared_var:
				shared_var.value += len(buff)
			f.write(buff)

def combine_files(parts, path):
	'''
	Function combines file parts.
	'''
	with open(path, 'wb') as output:
		for part in parts:
			with open(part, 'rb') as f:
				output.writelines(f.readlines())
			os.remove(part)
				
if __name__ == "__main__":
	import logger, pdb
	log = logger.create_debugging_logger()

	url = r"http://a.tumblr.com/tumblr_mayhwb20BD1rgwlgjo1_r2.mp3"
	url = r"http://soundcloud.com/johnnyconcept/winter-mainroom-sessions-2012/download"
	url = r"http://api.ning.com/files/ircV2rFsD-WS5YkvKlBudV18aLi24lZHD6w7dqA81zp4f9KtPYPTGiQK20Og9lAm/MakeHerSay.mp3"

	obj = SmartDL(url, show_output=True, logger=log)
	print obj.isFinished()
	obj.start()
	pdb.set_trace()
	
	for i in range(10):
		print "speed is %d" % obj.get_speed()
		print "time is %d" % obj.get_dl_time()
		time.sleep(1)
		if obj.isFinished():
			break
		
	print "Waiting for download to be completed..."
	obj.wait()
	print "time is %d" % obj.get_dl_time()
	obj.wait()
	
	# os.startfile(obj.get_dest())
	
	pdb.set_trace()