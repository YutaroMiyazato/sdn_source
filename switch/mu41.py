import time

from ryu.base import app_manager

from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls

from ryu.ofproto import ofproto_v1_3
from ryu.ofproto import ether
from ryu.ofproto import inet

from ryu.lib import hub

from ryu.lib.packet import packet
from ryu.lib.packet import vlan
from ryu.lib.packet import cfm
from ryu.lib.packet import lldp
from ryu.lib.packet import ipv4
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types

from ryu import utils
import time

# <--- db declaration
import peewee
import MySQLdb

db = peewee.MySQLDatabase("ryu_db", host="mat.ns.ie.u-ryukyu.ac.jp", port=3306, user="root", passwd="")

class Topology(peewee.Model):
    id = peewee.IntegerField()
    dport1 = peewee.CharField()
    dport2 = peewee.CharField()
    delay = peewee.FloatField()
    judge = peewee.CharField()

    class Meta:
        database = db
# --->

class Switch13(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(Switch13, self).__init__(*args, **kwargs)
        # initialize mac address table.
        self.mac_to_port = {}
        self.datapaths = []
        self.hw_addr = '88:d7:f6:7a:34:90'
        self.ip_addr = '10.50.0.100'
        self.vlan_type=ether.ETH_TYPE_8021Q
        self.ipv4_type=ether.ETH_TYPE_IP
        self.lldp_type=ether.ETH_TYPE_LLDP
        self.cfm_type=ether.ETH_TYPE_CFM
        self.lldp_thread = hub.spawn(self.lldp_loop)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        self.datapaths.append(datapath)

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,ofproto.OFPCML_NO_BUFFER)]

        self.add_flow(datapath, 0, match, actions)

        self.send_port_desc_stats_request(datapath)
    
    def lldp_loop(self):
        while True:
            for dp in self.datapaths:
                self.send_port_desc_stats_request(dp)
            hub.sleep(10)

    def send_port_desc_stats_request(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        req = parser.OFPPortDescStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
 
        # construct flow_mod message and send it.
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                match=match, instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc_stats_reply_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        
        for stat in ev.msg.body:
            if stat.port_no < ofproto.OFPP_MAX:
                timestamp = time.time()
                print "datapath:", datapath.id
                print "prot:", stat.port_no
                print "hard:", stat.hw_addr
                self.send_lldp_packet(datapath, stat.port_no, stat.hw_addr, timestamp)
 
    
    def send_lldp_packet (self, datapath, port_no, hw_addr, timestamp):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=self.lldp_type, src=hw_addr, dst=lldp.LLDP_MAC_NEAREST_BRIDGE))
 
        tlv_chassis_id = lldp.ChassisID(subtype=lldp.ChassisID.SUB_LOCALLY_ASSIGNED, chassis_id=str(datapath.id))
        tlv_port_id = lldp.PortID(subtype=lldp.PortID.SUB_LOCALLY_ASSIGNED, port_id=str(port_no))
        tlv_ttl = lldp.TTL(ttl=10)
        tlv_time = lldp.TimeStamp(timestamp=timestamp)
        tlv_end = lldp.End()
        tlvs = (tlv_chassis_id, tlv_port_id, tlv_ttl, tlv_time, tlv_end)
        pkt.add_protocol(lldp.lldp(tlvs))
        pkt.serialize()
        data = pkt.data

        actions = [parser.OFPActionOutput(port_no)]
        out = parser.OFPPacketOut(datapath=datapath,
                                  buffer_id=ofproto.OFP_NO_BUFFER,
                                  in_port=ofproto.OFPP_CONTROLLER,
                                  actions=actions,
                                  data=data)
        datapath.send_msg(out)
    
    
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        port = msg.match['in_port']
        pkt = packet.Packet(data=msg.data)
 
        pkt_ethernet = pkt.get_protocol(ethernet.ethernet)
        if not pkt_ethernet:
            return
 
        pkt_lldp = pkt.get_protocol(lldp.lldp)
        if pkt_lldp:
            self.handle_lldp(datapath, port, pkt_lldp)
    
    def handle_lldp(self, datapath, port, pkt_lldp):
        timestamp_diff = time.time() - pkt_lldp.tlvs[3].timestamp
        print "datapath:", datapath.id, "port:", port, "datapath:", pkt_lldp.tlvs[0].chassis_id, "port:", pkt_lldp.tlvs[1].port_id, "delay", timestamp_diff


        # <--- db select
        if datapath.id is not None:

            if int(datapath.id) > int(pkt_lldp.tlvs[0].chassis_id):
                sid1 = str(pkt_lldp.tlvs[0].chassis_id) + "-" + str(pkt_lldp.tlvs[1].port_id)
                sid2 = str(datapath.id) + "-" + str(port)
            else:
                sid1 = str(datapath.id) + "-" + str(port)
                sid2 = str(pkt_lldp.tlvs[0].chassis_id) + "-" + str(pkt_lldp.tlvs[1].port_id)
            
            print sid1 + " , " + sid2
            hoge = Topology.select().where((Topology.dport1 == sid1) & (Topology.dport2 == sid2)) 
            if hoge.exists():
                print "update"
                # <--- db update
                if int(datapath.id) > int(pkt_lldp.tlvs[0].chassis_id):
                    topo = Topology.update(delay=timestamp_diff).where((Topology.dport1 == sid1) & (Topology.dport2 == sid2))
                    topo.execute()
                else:
                    topo = Topology.update(delay=timestamp_diff).where((Topology.dport1 == sid1) & (Topology.dport2 == sid2))
                    topo.execute()
                # db update --->
            else:
                # <--- db insert
                print "insert"
                if int(datapath.id) > int(pkt_lldp.tlvs[0].chassis_id):
                    dport1 = str(pkt_lldp.tlvs[0].chassis_id) + "-" + str(pkt_lldp.tlvs[1].port_id)
                    dport2 = str(datapath.id) + "-" + str(port)
                    topo = Topology.insert(dport1=dport1,dport2=dport2,delay=timestamp_diff,judge='S')
                    topo.execute()
                else:
                    dport1 = str(datapath.id) + "-" + str(port)
                    dport2 = str(pkt_lldp.tlvs[0].chassis_id) + "-" + str(pkt_lldp.tlvs[1].port_id)
                    topo = Topology.insert(dport1=dport1,dport2=dport2,delay=timestamp_diff,judge='S')
                    topo.execute()
                # db insert --->