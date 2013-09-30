#!/usr/bin/python
#
# The task daemon, which runs jobs on remote machines.
# Author: Giorgio Di Guardia
# Date: Sept 2012
#
# There are several things in this code that need cleaned up:
# 1) Should be more modular (ssh commands should not be run
#    in the scheduling loop), it could call a function on the machine config object
#    This change would make it easier to get the whole system running on a windows machine.
# 2) Likely, we don't need to use a database to store the current state.
# 3) Arguments shouldn't be passed around as a list of unstructured data, 
#    we can put these in a structure for safer access.
# 4) We should not use things like os.system to call unix specific commands (mkdir, chmod)
#    Here we should use the python equivalent instead.
#
#

import time
import getopt
import sys
import os
from os import path
import threading
import restlite
import random
from subprocess import Popen, PIPE, STDOUT
import commands

haveNova = False
try:
	from novaclient.v1_1 import client
	from novaclient import exceptions as novaexceptions
	haveNova = True
except:
	haveNova = False;
	
from collections import deque
import sqlite3
import time


###  CONFIGURATION VARIABLES  ######
username = "admin"
password = "Princeton1"
tenant = "admin"
authurl = "http://127.0.0.1:5000/v2.0/"
port = 8000
cloudHost="10.0.0.1"
sshKeyFile="/var/www/html/onlinerender/setup/oskey.priv"
sshUser="ec2-user"
modelRoot='/mnt/CloudMirror'
cloudRoot='/mnt/CloudShare'
serverCloudRoot='/var/www/html/onlinerender'
dbFile = 'db.db'
###      END OF CONFIG VAR    ######

class BaseMachine:
	def __init__(self):
		pass

class LocalMachine(BaseMachine):
	def __init__(self, arch = "Linux-x86_64", modelRoot = modelRoot, cloudRoot = cloudRoot):
		print 'Creating LocalMachine with %s\n' % arch
		self.cloudRoot = cloudRoot
		self.modelRoot = modelRoot
		self.arch = arch
		self.ip = "Localhost"
		
	def checkConfig(self):
		print 'Checking config on local machine ....\n'

		cloudRootExists = path.isdir(self.cloudRoot)
		modelRootExists = modelRootExists = path.isdir(self.modelRoot)
		print 'Cloud root:"%s"  %d\n' % (self.cloudRoot, cloudRootExists)
		print 'Model root:"%s"  %d\n' % (self.modelRoot, modelRootExists)
		
		self.canAccessModels = cloudRootExists and modelRootExists
		self.sshTime = 0
		print '.... can access models = %d, ssh took %f' % (self.canAccessModels, self.sshTime)

	def getCommand(self, commandMap):	
		exe = self.cloudRoot + '/models/%(model_name)s/%(version)d/bin/IDTKCloudDetection' % commandMap
		
		commandMap['detectionDir'] = self.cloudRoot + '/' + commandMap['detectionDir']
		commandMap['exe'] = exe
		
#		commandMap['cloudHost'] = self.cloudHost
		commandMap['keyArg'] = ''
		commandMap['cloudRoot'] = self.cloudRoot
		
		
		return ('"%(exe)s" -network "%(cloudRoot)s%(modelFile)s" -image "%(cloudRoot)s/data/%(image_id)d.hdr" -outputDir "%(detectionDir)s" -image_id %(image_id)d -modelName %(model_name)s -model_version %(version)d -user %(user_id)d  > "%(logFile)s"' % commandMap)

	def __repr__(self):
		return repr(self.__dict__)
		

class Machine(BaseMachine):
	def __init__(self, ip = None, arch = "Linux-x86_64", modelRoot = modelRoot, cloudRoot = cloudRoot, sshUser = sshUser, sshKeyFile = sshKeyFile, cloudHost = cloudHost):
		self.ip = ip
		self.arch = arch
		self.modelRoot = modelRoot
		self.cloudRoot = cloudRoot
		self.sshUser = sshUser
		self.sshKeyFile = sshKeyFile
		self.cloudHost = cloudHost

		self.sshTime = -1
		self.canAccessModels = False
	
	def getCommand(self, commandMap):	
		exe = self.cloudRoot + '/models/%(model_name)s/%(version)d/bin/IDTKCloudDetection' % commandMap
		
		commandMap['detectionDir'] = self.cloudRoot + commandMap['detectionDir']
		commandMap['exe'] = exe
		
		commandMap['cloudHost'] = self.cloudHost
		commandMap['keyArg'] = ''
		commandMap['sshUser'] = self.sshUser
		commandMap['cloudRoot'] = self.cloudRoot

		if len(self.sshKeyFile):
			commandMap['keyArg'] = '-i %s' % self.sshKeyFile
	
		return ('ssh %(keyArg)s %(sshUser)s@%(ip)s env IDTKCloudHost=%(cloudHost)s %(exe)s -network %(cloudRoot)s%(modelFile)s -image %(cloudRoot)s/data/%(image_id)d.hdr -outputDir %(detectionDir)s -image_id %(image_id)d -modelName %(model_name)s -model_version %(version)d -user %(user_id)d  > %(logFile)s' % commandMap)


	def checkConfig(self):
		print 'Checking config on %s ....' % self.ip

		sshStr = 'ssh -i %s %s@%s' % (self.sshKeyFile, self.sshUser, self.ip)

		startTime = time.time()
		testModelAccess = sshStr + ' [ -d %s ] > /dev/null 2>&1' % ( self.modelRoot )
		self.canAccessModels = os.system(testModelAccess) == 0		
		self.sshTime = time.time() - startTime
		
		print '.... %s can access models = %d, ssh took %f' % (self.ip, self.canAccessModels, self.sshTime)

	def __str__(self):
		return '[%s, %s]'% (self.ip, self.arch)

	def __repr__(self):
		return repr(self.__dict__)
	


	
jobsdone = []
taskid=0
batchid=0
tasks = []
batchs = []
lock = threading.Lock()
joblist = []

def initializeWithOpenStack():
	osconn = client.Client(username, password, tenant, authurl, service_type="compute")
	servers = osconn.servers.list()
	flavors = osconn.flavors.list()
	images = osconn.images.list()
	servers = osconn.servers.list()
	ips = []
	for s in servers:
		print modelRoot
		ips.append(Machine(s.networks['demonet'][0], 'Linux-x86_64', modelRoot = modelRoot))
	return ips


print sys.argv
optlist, args = getopt.getopt(sys.argv[1:], 's:u:',['servers=', 'user=', 'sshKeyFile=', 'cloudRoot=', 'modelRoot=', 'cloudHost=', 'openstack', 'serverCloudRoot='])
ips = []

for o, a in optlist:
	if o in ("s", "--servers"):
		ips = eval(a)
		for i in xrange(0, len(ips)):
			if not isinstance(ips[i], BaseMachine):
				ips[i] = Machine(ips[i], 'Linux-x86_64', cloudRoot = cloudRoot, sshKeyFile = sshKeyFile, sshUser = sshUser, cloudHost = cloudHost, modelRoot = modelRoot)

	elif o in ("u", "--user"):
		sshUser = a
	elif o in ("--sshKeyFile"):
		sshKeyFile = a
	elif o in ("--cloudRoot"):
		cloudRoot = a
	elif o in ("--modelRoot"):
		modelRoot = a
	elif o in ("--cloudHost"):
		cloudHost = a
	elif o in ("--openstack"):
		if haveNova:
			ips = ips + initializeWithOpenStack()
	elif o in ("--serverCloudRoot"):
		serverCloudRoot = a;
		print 'Server cloud root:' + serverCloudRoot
	
			
if len(ips) == 0:
	if haveNova:
		ips = initializeWithOpenStack()

for i in ips:
	i.checkConfig()

freeips = deque(ips)


#SQLite3 
try:
	os.remove('db.db')
except OSError:
	pass
conn = sqlite3.connect("db.db")
cursor = conn.cursor()
try:
	cursor.execute("""CREATE TABLE if not exists tasks
	                  (jobid int primary key, imageid int, user int, model text, version int, status text, exitcode int, server text, priority int, output text, pid int, arch varchar(32))
	               """)
except OperationalError:
	print "Existing table"


def queueToList(queue):
        tmp = []
        n=0
        while True:
                try:
                        tmp.append(queue[n])
                        n=n+1
                except IndexError:
                        return tmp
                        break
def log(msg):
	print msg
	lock.acquire()
	f = open('./log.txt', 'a')
	f.write(msg)
	f.write('\n')
	f.close()
	lock.release()

log('***** Server started ' + time.asctime( time.localtime(time.time()) ) + ' on port %d ' % port)
log('***** Servers: ' + ', '.join([str(i) for i in ips]))
log('***** sshUser: ' + sshUser)
log('***** sshKeyFile: \"' + sshKeyFile + '\"')

class DoDetection(threading.Thread):
    def __init__(self, machine, command, lock):
        threading.Thread.__init__(self)
        self.machine = machine
	self.command = command
	self.lock = lock
    def run(self):
	start_time = time.time()

	commandMap = {
		'ip': self.machine.ip,
		'model_name' : self.command[1],
		'jobid' : self.command[2],
		'image_id' : self.command[3],
		'user_id' : self.command[4],
		'version' : self.command[5] }

	modelFile = '/models/%(model_name)s/%(version)d/model/model.xsg' % commandMap
	detectionDir = '/data/detections/%(user_id)d/%(image_id)d/%(model_name)s/%(version)d' % commandMap
	serverDetectionDir = serverCloudRoot + detectionDir

	dataDir = '/data/detections/'
	serverDataDir = serverCloudRoot + '/data/detections/'

	commandMap['detectionDir'] = detectionDir
	commandMap['modelFile'] = modelFile
	commandMap['dataDir'] = dataDir
	commandMap['logFile'] = serverDetectionDir + '/detect.log'

	try:
		os.makedirs(serverDetectionDir)
	except:
		pass

	if not path.exists(serverDetectionDir):
		log('--- Error creating detection dir: %s\n --- Possible problem with permissions!' % detectionDir)

	# Fix these to use the system commands...
	os.system('chown -R apache:apache %s' % (serverDataDir))  #change directory permissions
	os.system('chown -R apache:apache %s' % (serverDetectionDir))
	os.system('chmod 775 %s' % (serverDetectionDir))

	runningstr = self.machine.getCommand(commandMap);
	filename = ('data%d.out' % (commandMap['jobid']))	# stdout logging

	try:
	        os.remove(filename)
	except OSError:
	        pass

	f=file(filename, 'w')
	log('--- Preparing to run Job: %s, %s' % (runningstr, self.command[6]))
	child = Popen(runningstr, stdout=f, stderr=STDOUT, shell=True)
	log('--- Job running: PID %d' % (child.pid))
	progoutput=list()
	#Update the PID to the DB
	lock.acquire()
	conn = sqlite3.connect(dbFile)
	cursor = conn.cursor()
	cursor.execute("UPDATE tasks SET pid=? WHERE jobid=?", (child.pid, commandMap['jobid']))
	conn.commit();
	lock.release()
	progoutput.append(child.wait())
	f.close()
	log('-- Job finished (pid: %d )' % (child.pid))
	progoutput.append(open(filename, 'r').read())
#	progoutput[1] = progoutput[1].replace('Warning: No xauth data; using fake authentication data for X11 forwarding.\r', '')
#	progoutput[1] = progoutput[1].replace('Warning: Permanently added \'' + commandMap['ip'] + '\' (RSA) to the list of known hosts.\r\n', '')
	try:
                os.remove(filename)
        except OSError:
                pass
	if (progoutput[1][:1]=="\n"):
		progoutput[1]=progoutput[1][1:]
	totsecs = time.time() - start_time
	totsecs = round(totsecs, 2)
	if (progoutput[0]==0):
	        log ('*** DONE %s, \t took: %.2f seconds \t [exit code: %d]\n' % (self.machine.ip, totsecs, progoutput[0]))
                conn = sqlite3.connect(dbFile)
                cursor = conn.cursor()
                cursor.execute("UPDATE tasks SET status='Completed', exitcode=? WHERE jobid=?", (progoutput[0], commandMap['jobid']))
                conn.commit();
	else:
		if (progoutput[0]==-9):
			log ('!!! KILLED %s, \t took: %.2f seconds \t [exit code: %d]\n' % (self.machine.ip, totsecs, progoutput[0]))
	                cursor.execute("UPDATE tasks SET status='Killed', exitcode=? WHERE jobid=?", (progoutput[0], commandMap['jobid']))
	                conn.commit();
		else:
			log ('!!! ERROR %s, \t took: %.2f seconds \t [exit code: %d]\n' % (self.machine.ip, totsecs, progoutput[0]))
	                conn = sqlite3.connect(dbFile)
	                cursor = conn.cursor()
	                cursor.execute("UPDATE tasks SET status='Error', exitcode=? WHERE jobid=?", (progoutput[0], commandMap['jobid']))
	                conn.commit();

	#Add the Job to the jobsdone List
	jobsdone.append([self.command, totsecs, progoutput[0], 'null'])
	#Add the Machine back to the Available Machines queue
	freeips.append(self.machine)

	# Checks if the Tasks are done (compares the IPS with the Free IPS list)
	tmp1 = queueToList(freeips)
	tmp2 = ips
	tmp1.sort()
	tmp2.sort()
	if (tmp1==tmp2):
		log ('--- All Tasks Done, Waiting')
	CheckJobs().start()

class CheckJobs(threading.Thread):
        def __init__(self):
                threading.Thread.__init__(self)
        def run(self):
#		global conn
                while True:
			todo = None
			for ti in xrange(0, len(tasks)):
				todo = tasks[ti]
				freemachine = None
				print todo

				for f in freeips:
					if f.arch == todo[6]:
						freemachine = f
				if freemachine: break
				else:
					todo = None
				log ('No machines available for task %d -- skipping' % ti)

			# No jobs can be run.
			if not todo: break

			tasks.pop(ti)
			freeips.remove(freemachine)
			lock.acquire()
			conn = sqlite3.connect(dbFile)
			cursor = conn.cursor()
			cursor.execute("UPDATE tasks SET status='Running', server=? WHERE jobid=?", (freemachine.ip, todo[2]))
			conn.commit();
			lock.release()
			log ('Task running@%s \t (%d remaining) \t [%s] \t [%d free]' % (freemachine, len(tasks),todo, len(freeips)))
			DoDetection(freemachine, todo, lock).start()


CheckJobs().start()

@restlite.resource
def listservers():
	def GET(request):
		s = [eval(repr(i)) for i in ips]
		return request.response(s)
	return locals()

@restlite.resource
def getlog():
	def GET(request):
	        f = open('./log.txt', 'r')
	        return f.read()		
	return locals()

@restlite.resource
def index():
	def GET(request):
		a=('<h1>Task Daemon</h1>Usage<br>GET /jobs\tlist of current jobs<br>GET /jobsdone\tlist of jobs already done<br>POST /jobs\tsubmit a job to the daemon')
		return a
	return locals()


@restlite.resource
def status():
        def GET(request):
                global model
                model.login(request)
                return('succesfully logged in')
        return locals()

@restlite.resource
def alljobs():
	def GET(request):
		lock.acquire()
		cursor = conn.cursor()
		cursor.execute("SELECT * FROM tasks where status!=?", ['Deleted'])
		rows = []
		jsonstr = []
		rows = cursor.fetchall()
		conn.close
		lock.release()
		str = ""
		for row in rows:
			#(taskid int, imageid int, user int, model text, version int, status text, exitcode int, server text, priority int, output text)
			str=row[9]
			str=str.replace("[", "")
			str=str.replace("]", "")
			str=str.replace(", ", ". ")
			jsonstr.append({ 'jobid': row[0], 'image_id': row[1], 'user': row[2], 'model': row[3], 'version': row[4], 'status': row[5], 'exitcode': row[6], 'server': row[7], 'priority': row[8], 'pid': row[10]})
		return request.response(jsonstr)
	def POST(request, entity):
		params=entity.split('=')
                #Get PID of the jobid from the db.
                lock.acquire()
                conn = sqlite3.connect(dbFile)
                cursor = conn.cursor()
                rows = []
                jsonstr = []
                cursor.execute ("SELECT * FROM tasks WHERE jobid=?", [params[1]])
                rows=cursor.fetchall()
                lock.release()
		for row in rows:
                        str=row[9]
                        str=str.replace("[", "")
                        str=str.replace("]", "")
                        str=str.replace(", ", ". ")
			jsonstr.append({ 'jobid': row[0], 'image_id': row[1], 'user': row[2], 'model': row[3], 'version': row[4], 'status': row[5], 'exitcode': row[6], 'server': row[7], 'priority': row[8], 'output': str, 'pid': row[10]})

		# Job ids are unique.  This query should return just one.
		if len(jsonstr) == 1:
			jsonstr = jsonstr[0]

		return request.response(jsonstr)
	return locals()

@restlite.resource
def jobsdonef():
	def GET(request):
		return request.response(('jobsdone', (jobsdone)))
	return locals()

@restlite.resource
def killjob():
	def GET(request):
		return request.response(('kill', (entity)))
	def POST(request, entity):
		params=entity.split('=')
		#Get PID of the jobid from the db.
		lock.acquire()
		conn = sqlite3.connect(dbFile)
		cursor = conn.cursor()
		cursor.execute ("SELECT * FROM tasks WHERE jobid=?", [params[1]])
		row=cursor.fetchone()
		lock.release()
		try:
		    row[0]
		except TypeError:
			return request.response(('error', 'no process found with the id specified'))
		#Check if the Job is running or completed.
                if (row[5]=="Canceled"):
                        return request.response(('error', 'already canceled.'))
                if (row[5]=="Killed"):
                        return request.response(('error', 'already killed.'))
                if (row[5]=="Deleted"):
                        return request.response(('error', 'cannot kill a deleted job.'))
		if (row[5]=="Completed"):
			return request.response(('error', 'process already completed.'))
		#Kill the JOB
		if (row[5]=="Running"):
			os.kill(row[10], 9)
		if (row[5]=="In Queue"):
                        lock.acquire()
                        cursor.execute("UPDATE tasks SET status=? WHERE jobid=?", (['Canceled', params[1]]))
                        conn.commit();
                        lock.release()
			# Remove the object QUEUE from tasks
			for i in range (0, len(tasks)):
				if (tasks[i][2]==int(params[1])):
					tasks.remove(tasks[i])
					break
		return request.response(('success', 'process killed'))
	return locals()		

@restlite.resource
def killbatch():
	def GET(request):
		return request.response(('kill', (entity)))
	def POST(request, entity):
		global batchs
		params=entity.split('=')
#		params[1]=int(params[1]) - 1
		#Get PID of the jobid from the db.
		lock.acquire()
		conn = sqlite3.connect(dbFile)
		cursor = conn.cursor()
		lock.release()
		print "preprint"
		print batchs[int(params[1])]
		for i in batchs[int(params[1])]:
			print "Batch kill, killing job %d " % i
			lock.acquire()
			cursor.execute ("SELECT * FROM tasks WHERE jobid=?", [i])
			row=cursor.fetchone()
			lock.release()
			try:
			    row[0]
			except TypeError:
				return request.response(('error', 'no process found with the id specified'))
			#Check if the Job is running or completed.
#	                if (row[5]=="Canceled"):
#	                        return request.response(('error', 'already canceled.'))
#	                if (row[5]=="Killed"):
#	                        return request.response(('error', 'already killed.'))
#	                if (row[5]=="Deleted"):
#	                        return request.response(('error', 'cannot kill a deleted job.'))
#			if (row[5]=="Completed"):
#				return request.response(('error', 'process already completed.'))
			#Kill the JOB
			if (row[5]=="Running"):
				os.kill(row[10], 9)
			if (row[5]=="In Queue"):
	                        lock.acquire()
	                        cursor.execute("UPDATE tasks SET status=? WHERE jobid=?", (['Canceled', params[1]]))
	                        conn.commit();
	                        lock.release()
				# Remove the object QUEUE from tasks
				for i in range (0, len(tasks)):
					if (tasks[i][2]==int(params[1])):
						tasks.remove(tasks[i])
						break
		return request.response(('success', 'batch killed'))
	return locals()		



@restlite.resource
def removejob():
	def GET(request):
		return request.response(('remove', (entity)))
	def POST(request, entity):
		params=entity.split('=')
		#Get PID of the jobid from the db.
		lock.acquire()
		conn = sqlite3.connect(dbFile)
		cursor = conn.cursor()
		cursor.execute ("SELECT * FROM tasks WHERE jobid=?", [params[1]])
		row=cursor.fetchone()
		lock.release()
		try:
			row[0]
		except TypeError:
			return request.response(('error', 'no job found with the id specified'))
		#Check if the Job is running/in queue, if yes, send an error.
		if (row[5]=="Running"):
			return request.response(('error', 'process is running.'))
		if (row[5]=="In Queue"):
			return request.response(('error', 'process is in queue.'))
	        if (row[5]=="Deleted"):
			return request.response(('error', 'process already deleted.'))
		lock.acquire()
		cursor.execute("UPDATE tasks SET status=? WHERE jobid=?", (['Deleted', params[1]]))
		conn.commit();
		lock.release()
		return request.response(('success', 'process removed'))
	return locals()		



@restlite.resource
def joblist():
        def GET(request):
                global directory
		tmp = queueToList(tasks)
		CheckJobs().start()
		return request.response(('jobs', (tasks)))
        def POST(request, entity):
		global taskid
		global cursor
		global conn
		params=entity.split('&')
		for i in range(0, len(params)):
			params[i]=params[i].split('=')
		params=dict(params)
		if (params['highpriority']!="off"):
			log("# RECEIVED HIGH PRIORITY TASK")
			priority=1
		else:
			priority=10
                if len(params['model'])>0:
			taskid +=1
                        tasks.append([priority, params['model'], taskid, int(params['id']), int(params['user']), int(params['version']), params['arch']])
			tasks.sort()
			CheckJobs().start()
			lock.acquire()
			cursor.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, 'In Queue', 0, '', ?, '', 0, ?)", (taskid, int(params['id']), int(params['user']), params['model'], params['version'], priority, params['arch']))
			conn.commit()
			lock.release()
			if (params['highpriority']!="off"):
				return request.response({ 'model': params['model'], 'jobid': taskid, 'image_id': int(params['id']), 'user': int(params['user']), 'version': int(params['version']), 'highpriority': 'yes' })
			else:
				return request.response({ 'model': params['model'], 'jobid': taskid, 'image_id': int(params['id']), 'user': int(params['user']), 'version': int(params['version']) })
		else:
                        return 'Error'
	return locals()

@restlite.resource
def batchjoblist():
        def GET(request):
                global directory
		tmp = queueToList(tasks)
		CheckJobs().start()
		return request.response(('jobs', (tasks)))
        def POST(request, entity):
		global taskid
		global batchid
		global cursor
		global conn
		params=entity.split('&')
		for i in range(0, len(params)):
			params[i]=params[i].split('=')
		params=dict(params)
		priority=11
		batchid +=1
		imglist=params['id'].split(',')
		batchs.append([])
		batchs.append([])
		for i in range (0, len(imglist)):
			if len(params['model'])>0:
				taskid +=1
				batchs[batchid].append(taskid)
				tasks.append([priority, params['model'], taskid, int(imglist[i]), int(params['user']), int(params['version']), params['arch']])
				tasks.sort()
				CheckJobs().start()
				lock.acquire()
				cursor.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, 'In Queue', 0, '', ?, '', 0, ?)", (taskid, int(imglist[i]), int(params['user']), params['model'], params['version'], priority, params['arch']))
				conn.commit()
				lock.release()
	                        #return request.response({ 'model': params['model'], 'jobid': taskid, 'image_id': int(imglist[i]), 'user': int(params['user']), 'version': int(params['version']), 'batch': 'yes' })
			else:
				return 'Error'
		return request.response({ 'model': params['model'], 'batchjobid': batchid, 'image_id': imglist, 'user': int(params['user']), 'version': int(params['version']), 'batch': 'yes' })
	return locals()


# List of all the possible routes
routes = [ 
    (r'GET /servers/', 'GET /servers/', listservers),
    (r'GET /log/', 'GET /log/', getlog),
    (r'GET /jobs/', 'GET /jobs/', joblist),
    (r'GET /jobsdone/', 'GET /jobsdone/', jobsdonef),
    (r'POST /jobs/(?P<task>.*)', 'POST /jobs/%(task)s', 'ACCEPT=application/json', joblist),
    (r'POST /batchjobs/(?P<task>.*)', 'POST /batchjobs/%(task)s', 'ACCEPT=application/json', batchjoblist),
    (r'POST /kill/(?P<task>.*)', 'POST /kill/%(task)s', 'ACCEPT=application/json', killjob),
    (r'POST /killbatch/(?P<task>.*)', 'POST /killbatch/%(task)s', 'ACCEPT=application/json', killbatch),
    (r'GET /alljobs/', 'GET /alljobs/', alljobs),
    (r'POST /alljobs/(?P<task>.*)', 'POST /alljobs/%(task)s', alljobs),
    (r'POST /removejob/(?P<task>.*)', 'POST /removejob/%(task)s', removejob),
    (r'GET /status/', status)
]

if __name__ == '__main__':
    import sys
    from wsgiref.simple_server import make_server
    httpd = make_server('', port, restlite.router(routes))
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass

