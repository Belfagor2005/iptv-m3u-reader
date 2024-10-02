from enigma import eServiceReference, eTimer, iPlayableService
from Screens.InfoBar import InfoBar, MoviePlayer
from Screens.InfoBarGenerics import saveResumePoints, resumePointCache, resumePointCacheLast, delResumePoint
from Components.config import config
from Components.ServiceEventTracker import ServiceEventTracker
from Components.Sources.Progress import Progress
from Components.Label import Label
from Components.MultiContent import MultiContentEntryPixmapAlphaBlend
from Components.ActionMap import HelpableActionMap
from Tools.Directories import resolveFilename, SCOPE_CURRENT_SKIN
from Tools.LoadPixmap import LoadPixmap
from .IPTVProcessor import constructCatchUpUrl
from .IPTVProviders import processService as processIPTVService
from time import time
import datetime
import re

try:
	from Components.EpgListGrid import EPGListGrid as EPGListGrid
except ImportError:
	EPGListGrid = None
try:
	from Screens.EpgSelectionGrid import EPGSelectionGrid as EPGSelectionGrid
except ImportError:
	EPGSelectionGrid = None


def injectCatchupInEPG():
	if EPGListGrid:
		if injectCatchupIcon not in EPGListGrid.buildEntryExtensionFunctions:
			EPGListGrid.buildEntryExtensionFunctions.append(injectCatchupIcon)
		__init_orig__ = EPGListGrid.__init__
		def __init_new__(self, *args, **kwargs):
			self.catchUpIcon = LoadPixmap(resolveFilename(SCOPE_CURRENT_SKIN, "epg/catchup.png"))
			if not self.catchUpIcon:
				self.catchUpIcon = LoadPixmap("/usr/lib/enigma2/python/Plugins/SystemPlugins/M3UIPTV/catchup.png")
			__init_orig__(self, *args, **kwargs)
		EPGListGrid.__init__ = __init_new__

	__old_EPGSelectionGrid_init__ = EPGSelectionGrid.__init__


	if EPGSelectionGrid:
		def __new_EPGSelectionGrid_init__(self, *args, **kwargs):
			EPGSelectionGrid.playArchiveEntry = playArchiveEntry
			__old_EPGSelectionGrid_init__(self, *args, **kwargs)
			self["CatchUpActions"] = HelpableActionMap(self, "MediaPlayerActions",
			{
				"play": (self.playArchiveEntry, _("Play Archive")),
			}, -2)

		EPGSelectionGrid.__init__ = __new_EPGSelectionGrid_init__

def injectCatchupIcon(res, obj, service, serviceName, events, picon, channel):
	r2 = obj.eventRect
	left = r2.left()
	top = r2.top()
	width = r2.width()
	if events:
		start = obj.timeBase
		end = start + obj.timeEpochSecs
		now = time()
		for ev in events:
			stime = ev[2]
			duration = ev[3]
			xpos, ewidth = obj.calcEventPosAndWidthHelper(stime, duration, start, end, width)
			if "catchupdays=" in service and stime < now and obj.catchUpIcon:
				pix_size = obj.catchUpIcon.size()
				pix_width = pix_size.width()
				pix_height = pix_size.height()
				match = re.search(r"catchupdays=(\d*)", service)
				catchup_days = int(match.groups(1)[0])
				if now - stime <= datetime.timedelta(days=catchup_days).total_seconds():
					res.append(MultiContentEntryPixmapAlphaBlend(
									pos=(left + xpos + ewidth - pix_width - 10, top + 10),
									size=(pix_width, pix_height),
									png=obj.catchUpIcon,
									flags=0))

class CatchupPlayer(MoviePlayer):
	def __init__(self, session, service, sref_ret="", slist=None, lastservice=None, event=None, orig_sref="", orig_url="", start_orig=0, end_org=0, duration=0, catchup_ref_type=4097):
		MoviePlayer.__init__(self, session, service=service, slist=slist, lastservice=lastservice)
		self.skinName = ["ArchiveMoviePlayer", "MoviePlayer"]
		self.onPlayStateChanged.append(self.__playStateChanged)
		self["progress"] = Progress()
		self.progress_change_interval = 1000
		self.catchup_ref_type = catchup_ref_type
		self.event = event
		self.orig_sref = orig_sref
		self.duration = duration
		self.orig_url = orig_url
		self.sref_ret = sref_ret
		self.start_orig = start_orig
		self.end_orig = end_org
		self.start_curr = start_orig
		self.duration_curr = duration
		self.progress_timer = eTimer()
		self.progress_timer.callback.append(self.onProgressTimer)
		self.progress_timer.start(self.progress_change_interval)
		self["progress"].value = 0
		self["time_info"] = Label("")
		self.onProgressTimer()
		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
			iPlayableService.evStart: self.__evServiceStart,
			iPlayableService.evEnd: self.__evServiceEnd,})


	def onProgressTimer(self):
		curr_pos = self.start_curr + self.getPosition()
		len = self.duration
		p = curr_pos - self.start_orig
		r = self.duration - p
		text = "+%d:%02d:%02d         %d:%02d:%02d         -%d:%02d:%02d" % (p / 3600, p % 3600 / 60, p % 60, len / 3600, len % 3600 / 60, len % 60, r / 3600, r % 3600 / 60, r % 60)
		self["time_info"].setText(text)
		progress_val = int((p / self.duration)*100)
		self["progress"].value = progress_val if progress_val >= 0 else 0

	def getPosition(self):
		seek = self.getSeek()
		if seek is None:
			return 0
		pos = seek.getPlayPosition()
		if pos[0]:
			return 0
		return pos[1] / 90000

	def __evServiceStart(self):
		if self.progress_timer:
			self.progress_timer.start(self.progress_change_interval)

	def __evServiceEnd(self):
		if self.progress_timer:
			self.progress_timer.stop()

	def __playStateChanged(self, state):
		playstateString = state[3]
		if playstateString == '>':
			self.progress_timer.start(self.progress_change_interval)
		elif playstateString == '||':
			self.progress_timer.stop()
		elif playstateString == 'END':
			self.progress_timer.stop()

	def destroy(self):
		if self.progress_timer:
			self.progress_timer.stop()
			self.progress_timer.callback.remove(self.onProgressTimer)

	def leavePlayer(self):
		self.setResumePoint()
		if self.progress_timer:
			self.progress_timer.stop()
			self.progress_timer.callback.remove(self.onProgressTimer)
		self.handleLeave("quit")

	def leavePlayerOnExit(self):
		if self.shown:
			self.hide()
		else:
			self.leavePlayer()

	def doSeekRelative(self, pts):
		pts = pts // 90000
		seekable = self.getSeek()
		if seekable is None:
			return
		prevstate = self.seekstate
		if self.seekstate == self.SEEK_STATE_EOF:
			if prevstate == self.SEEK_STATE_PAUSE:
				self.setSeekState(self.SEEK_STATE_PAUSE)
			else:
				self.setSeekState(self.SEEK_STATE_PLAY)
		#seekable.seekRelative(pts < 0 and -1 or 1, abs(pts))
		curr_pos = self.start_curr + self.getPosition()
		self.start_curr = curr_pos + pts
		atStart = False
		if self.start_curr < self.start_orig:
			self.start_curr = self.start_orig
			atStart = True

		if self.start_curr > self.start_orig + self.duration:
			self.session.nav.stopService()

		if atStart:
			self.duration_curr = self.duration
		else:
			self.duration_curr -= pts
		sref_split = self.sref_ret.split(":")
		sref_ret = sref_split[10:][0]
		url = constructCatchUpUrl(self.orig_sref, sref_ret, self.start_curr, self.start_curr+self.duration_curr, self.duration_curr)
		newPlayref = eServiceReference(self.catchup_ref_type, 0, url)
		newPlayref.setName(self.event.getEventName())
		self.session.nav.playService(newPlayref)
		self.onProgressTimer()
		if abs(pts*90000) > 100 and config.usage.show_infobar_on_skip.value:
			self.showAfterSeek()

	def setResumePoint(self):
		global resumePointCache, resumePointCacheLast
		service = self.session.nav.getCurrentService()
		ref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if (service is not None) and (ref is not None):
			seek = service.seek()
			if seek:
				pos = seek.getPlayPosition()
				if not pos[0]:
					key = ref.toString()
					lru = int(time())
					sl = seek.getLength()
					if sl:
						sl = sl[1]
					else:
						sl = None
					resumePointCache[key] = [lru, pos[1], sl]
					saveResumePoints()
	
	def doEofInternal(self, playing):
		if not self.execing:
			return
		if not playing:
			return
		ref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if ref:
			delResumePoint(ref)
		self.handleLeave("quit")

	def up(self):
		pass

	def down(self):
		pass


def playArchiveEntry(self):
	now = time()
	event, service = self["list"].getCurrent()[:2]
	playref, old_ref, is_dynamic, catchup_ref_type = processIPTVService(service, None, event)
	sref = playref.toString()
	if event is not None:
		stime = event.getBeginTime()
		if "catchupdays=" in service.toString() and stime < now:
			match = re.search(r"catchupdays=(\d*)", service.toString())
			catchup_days = int(match.groups(1)[0])
			if now - stime <= datetime.timedelta(days=catchup_days).total_seconds():
				duration = event.getDuration()
				sref_split = sref.split(":")
				url = sref_split[10:][0]
				url = constructCatchUpUrl(service.toString(), url, stime, stime+duration, duration)
				playref = eServiceReference(catchup_ref_type, 0, url)
				playref.setName(event.getEventName())
				infobar = InfoBar.instance
				if infobar:
					LastService = self.session.nav.getCurrentlyPlayingServiceOrGroup()
					self.session.open(CatchupPlayer, playref, sref_ret=sref, slist=infobar.servicelist, lastservice=LastService, event=event, orig_url=url, start_orig=stime, end_org=stime+duration, duration=duration, catchup_ref_type=catchup_ref_type, orig_sref=service.toString())