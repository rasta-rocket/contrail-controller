/*
 * Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
 */

#include "base/os.h"

#include "testing/gunit.h"

#include <base/logging.h>
#include <io/event_manager.h>
#include <tbb/task.h>
#include <base/task.h>

#include <cmn/agent_cmn.h>

#include "test_cmn_util.h"
#include "cfg/cfg_init.h"
#include "oper/operdb_init.h"
#include "controller/controller_init.h"
#include "pkt/pkt_init.h"
#include "services/services_init.h"
#include "vrouter/ksync/ksync_init.h"
#include "oper/interface_common.h"
#include "oper/nexthop.h"
#include "route/route.h"
#include "oper/vrf.h"
#include "oper/mpls.h"
#include "oper/vm.h"
#include "oper/vn.h"
#include "oper/path_preference.h"
#include "filter/acl.h"
#include "vr_types.h"
#include <controller/controller_export.h>
#include <ksync/ksync_sock_user.h>
#include <boost/assign/list_of.hpp>

using namespace boost::assign;

#define NULL_VRF ""
#define ZERO_IP "0.0.0.0"
#define ZERO_MAC "00:00:00:00:00:00"
MacAddress zero_mac;

void RouterIdDepInit(Agent *agent) {
}

struct PortInfo input[] = {
    {"intf1", 1, "1.1.1.1", "00:00:00:01:01:01", 1, 1, "fd10::2"},
    {"intf2", 2, "1.1.1.2", "00:00:00:01:01:02", 1, 2, "fd10::3"},
};
IpamInfo ipam_info[] = {
    {"1.1.1.0", 24, "1.1.1.10"},
    {"2.2.2.0", 24, "2.2.2.10"}
};

class TestAap : public ::testing::Test {
public:
    TestAap() {
        agent_ = Agent::GetInstance();
        peer_ = CreateBgpPeer(Ip4Address(1), "BGP Peer 1");
    }

    ~TestAap() {
        DeleteBgpPeer(peer_);
    }

    void AddVlan(std::string intf_name, int intf_id, uint32_t vlan) {
        std::ostringstream buf;
        buf << "<virtual-machine-interface-properties>";
        buf << "<sub-interface-vlan-tag>";
        buf << vlan;
        buf << "</sub-interface-vlan-tag>";
        buf << "</virtual-machine-interface-properties>";
        char cbuf[10000];
        strcpy(cbuf, buf.str().c_str());
        AddNode("virtual-machine-interface", intf_name.c_str(),
                intf_id, cbuf);
        client->WaitForIdle();
    }

    virtual void SetUp() {
        CreateVmportEnv(input, 2);
        client->WaitForIdle();
        EXPECT_TRUE(VmPortActive(1));
        EXPECT_TRUE(VmPortActive(2));
        AddIPAM("vn1", ipam_info, 2);
        client->WaitForIdle();
        seq_no = 1;
    }

    virtual void TearDown() {
        DeleteVmportEnv(input, 2, true);
        client->WaitForIdle();
        EXPECT_FALSE(VmPortFindRetDel(1));
        EXPECT_FALSE(VrfFind("vrf1", true));
        client->WaitForIdle();
        DelIPAM("vn1");
        client->WaitForIdle();
    }

    void FlapRoute(const Ip4Address ip, const VmInterface *vm_intf,
                   uint32_t flap_count) {
        InetUnicastRouteEntry *rt = RouteGet("vrf1", ip, 32);
        const AgentPath *path = rt->FindPath(vm_intf->peer());

        for (uint32_t i = 0; i <= flap_count; i++) {
            Agent::GetInstance()->oper_db()->route_preference_module()->
                EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                                   vm_intf->vm_mac());
            client->WaitForIdle();
            EXPECT_TRUE(path->path_preference().sequence() == seq_no++);
            EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
            EXPECT_TRUE(path->path_preference().ecmp() == false);
            EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

            Ip4Address server_ip = Ip4Address::from_string("10.1.1.3");
            TunnelType::TypeBmap bmap = (1 << TunnelType::MPLS_GRE);
            PathPreference path_preference(seq_no++, PathPreference::HIGH, false, false);
            Inet4TunnelRouteAdd(peer_, "vrf1", ip, 32, server_ip, bmap,
                    16, "vn1", SecurityGroupList(), path_preference);
            client->WaitForIdle();
            if (i < PathPreferenceSM::kMaxFlapCount) {
                EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
                EXPECT_TRUE(path->path_preference().ecmp() == false);
                EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);
            }
        }
    }

protected:
    BgpPeer *peer_;
    Agent *agent_;
    uint32_t seq_no;
};

//Add and delete allowed address pair route
TEST_F(TestAap, AddDel_1) {
    Ip4Address ip = Ip4Address::from_string("10.10.10.10");
    std::vector<Ip4Address> v;
    v.push_back(ip);

    AddAap("intf1", 1, v);
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));
    EXPECT_TRUE(RouteGet("vrf1", ip, 32)->GetActiveNextHop()->GetType() == 
                NextHop::INTERFACE);
    v.clear();
    AddAap("intf1", 1, v);
    EXPECT_FALSE(RouteFind("vrf1", ip, 32));
}

TEST_F(TestAap, AddDel_2) {
    Ip4Address ip = Ip4Address::from_string("10.10.10.10");
    std::vector<Ip4Address> v;
    v.push_back(ip);

    AddAap("intf1", 1, v);
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));

    DelLink("virtual-machine-interface-routing-instance", "intf1",
            "routing-instance", "vrf1");
    client->WaitForIdle();
    EXPECT_FALSE(RouteFind("vrf1", ip, 32));

    AddLink("virtual-machine-interface-routing-instance", "intf1",
            "routing-instance", "vrf1");
    client->WaitForIdle();
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));
}

TEST_F(TestAap, Update) {
    Ip4Address ip1 = Ip4Address::from_string("10.10.10.10");
    Ip4Address ip2 = Ip4Address::from_string("11.10.10.10");
    std::vector<Ip4Address> v;
    v.push_back(ip1);

    AddAap("intf1", 1, v);
    EXPECT_TRUE(RouteFind("vrf1", ip1, 32));

    v.push_back(ip2);
    AddAap("intf1", 1, v);
    EXPECT_TRUE(RouteFind("vrf1", ip1, 32));
    EXPECT_TRUE(RouteFind("vrf1", ip2, 32));

    v.clear();
    AddAap("intf1", 1, v);
    EXPECT_FALSE(RouteFind("vrf1", ip1, 32));
    EXPECT_FALSE(RouteFind("vrf1", ip2, 32));
}

//Check if subnet gateway for allowed address pait route gets set properly
TEST_F(TestAap, SubnetGw) {
    Ip4Address ip1 = Ip4Address::from_string("10.10.10.10");
    std::vector<Ip4Address> v;
    v.push_back(ip1);

    AddAap("intf1", 1, v);
    EXPECT_TRUE(RouteFind("vrf1", ip1, 32));

    IpamInfo ipam_info[] = {
        {"10.10.10.0", 24, "10.10.10.200", true},
    };
    AddIPAM("vn1", ipam_info, 1, NULL, "vdns1");
    client->WaitForIdle();

    Ip4Address subnet_service_ip = Ip4Address::from_string("10.10.10.200");
    InetUnicastRouteEntry *rt = RouteGet("vrf1", ip1, 32);
    EXPECT_TRUE(rt->GetActivePath()->subnet_service_ip() == subnet_service_ip);

    DelIPAM("vn1", "vdns1");
    client->WaitForIdle();
}

//Check if subnet gateway for allowed address pait route gets set properly
TEST_F(TestAap, EvpnRoute) {
    Ip4Address ip = Ip4Address::from_string("10.10.10.10");
    MacAddress mac("0a:0b:0c:0d:0e:0f");

    VmInterface *vm_intf = static_cast<VmInterface *>(VmPortGet(1));
    AddAap("intf1", 1, ip, mac.ToString());
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));
    EXPECT_TRUE(EvpnRouteGet("vrf1", mac, ip, 0));
    EXPECT_TRUE(vm_intf->allowed_address_pair_list().list_.size() == 1);

    InetUnicastRouteEntry *rt = RouteGet("vrf1", ip, 32);
    uint32_t label = rt->GetActivePath()->label();
    EXPECT_TRUE(label != vm_intf->label());
    const InterfaceNH *intf_nh =
        dynamic_cast<const InterfaceNH *>(rt->GetActiveNextHop());
    EXPECT_TRUE(intf_nh->GetDMac() == mac);

    AddAap("intf1", 1, Ip4Address(0), zero_mac.ToString());
    EXPECT_FALSE(RouteFind("vrf1", ip, 32));
    EXPECT_FALSE(EvpnRouteGet("vrf1", mac, ip, 0));
    EXPECT_TRUE(vm_intf->allowed_address_pair_list().list_.size() == 0);
    EXPECT_TRUE(agent_->mpls_table()->FindMplsLabel(label) == NULL);
}

//Check if subnet gateway for allowed address pair route gets set properly
TEST_F(TestAap, EvpnRoute_1) {
    Ip4Address ip = Ip4Address::from_string("10.10.10.10");
    MacAddress mac("0a:0b:0c:0d:0e:0f");

    VmInterface *vm_intf = static_cast<VmInterface *>(VmPortGet(1));
    AddAap("intf1", 1, ip, mac.ToString());
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));
    EXPECT_TRUE(EvpnRouteGet("vrf1", mac, ip, 0));
    EXPECT_TRUE(vm_intf->allowed_address_pair_list().list_.size() == 1);

    //Make VN as layer2 only
    AddL2Vn("vn1", 1);
    client->WaitForIdle();
    EXPECT_FALSE(RouteFind("vrf1", ip, 32));
    EXPECT_FALSE(EvpnRouteGet("vrf1", mac, ip, 0));
    EXPECT_FALSE(EvpnRouteGet("vrf1", mac, Ip4Address(0), 0));
    EXPECT_TRUE(vm_intf->allowed_address_pair_list().list_.size() == 1);
    AddAap("intf1", 1, Ip4Address(0), zero_mac.ToString());
    EXPECT_TRUE(vm_intf->allowed_address_pair_list().list_.size() == 0);
}

TEST_F(TestAap, EvpnRoute_with_mac_change) {
    Ip4Address ip = Ip4Address::from_string("10.10.10.10");
    MacAddress mac("0a:0b:0c:0d:0e:0f");
    MacAddress mac1("0a:0b:0c:0d:0e:0e");

    VmInterface *vm_intf = static_cast<VmInterface *>(VmPortGet(1));
    AddAap("intf1", 1, ip, mac.ToString());
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));
    EXPECT_TRUE(EvpnRouteGet("vrf1", mac, ip, 0));
    EXPECT_TRUE(vm_intf->allowed_address_pair_list().list_.size() == 1);

    InetUnicastRouteEntry *rt = RouteGet("vrf1", ip, 32);
    uint32_t label = rt->GetActivePath()->label();
    EXPECT_TRUE(label != vm_intf->label());
    const InterfaceNH *intf_nh =
        dynamic_cast<const InterfaceNH *>(rt->GetActiveNextHop());
    EXPECT_TRUE(intf_nh->GetDMac() == mac);

    AddAap("intf1", 1, ip, mac1.ToString());
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));
    EXPECT_FALSE(EvpnRouteGet("vrf1", mac, ip, 0));
    EXPECT_TRUE(EvpnRouteGet("vrf1", mac1, ip, 0));

    rt = RouteGet("vrf1", ip, 32);
    label = rt->GetActivePath()->label();
    EXPECT_TRUE(label != vm_intf->label());
    intf_nh = dynamic_cast<const InterfaceNH *>(rt->GetActiveNextHop());
    EXPECT_TRUE(intf_nh->GetDMac() == mac1);
}

#if 0
TEST_F(TestAap, EvpnRoute_3) {
    struct PortInfo input[] = {
        {"vnetOnIntf1", 2, "2.1.1.1", "00:00:00:02:01:01", 1, 1},
    };

    CreateVmportEnv(input, 1);
    client->WaitForIdle();

    AddVlan("vnetOnIntf1", 2, 1);
    AddLink("virtual-machine-interface", "intf1",
            "virtual-machine-interface", "vnetOnIntf1");
    client->WaitForIdle();

    Ip4Address ip = Ip4Address::from_string("2.1.1.1");
    MacAddress mac("00:00:00:02:01:01");

    VmInterface *vm_intf = static_cast<VmInterface *>(VmPortGet(2));
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));
    EXPECT_TRUE(EvpnRouteGet("vrf1", mac, ip, 1));

    //Enqueue traffic seen on vlan tag 0, and ensure path
    //preference doesnt get increased since vmi is sitting on
    //vlan tag 1
    EvpnRouteEntry *rt = EvpnRouteGet("vrf1", mac, ip, 0);
    const AgentPath *path = rt->FindPath(vm_intf->peer());

     Agent::GetInstance()->oper_db()->route_preference_module()->
        EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                           mac);
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

     Agent::GetInstance()->oper_db()->route_preference_module()->
        EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                           mac);
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 1);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);
}
#endif

TEST_F(TestAap, EvpnRoute_4) {
    //Have MAC address of AAP same as interface mac
    Ip4Address ip = Ip4Address::from_string("10.10.10.10");
    MacAddress mac("00:00:00:01:01:01");

    VmInterface *vm_intf = static_cast<VmInterface *>(VmPortGet(1));
    AddAap("intf1", 1, ip, mac.ToString());
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));
    EXPECT_TRUE(EvpnRouteGet("vrf1", mac, ip, 0));
    EXPECT_TRUE(vm_intf->allowed_address_pair_list().list_.size() == 1);

    InetUnicastRouteEntry *rt = RouteGet("vrf1", ip, 32);
    uint32_t label = rt->GetActivePath()->label();
    EXPECT_TRUE(label == vm_intf->label());
    const InterfaceNH *intf_nh =
        dynamic_cast<const InterfaceNH *>(rt->GetActiveNextHop());
    EXPECT_TRUE(intf_nh->GetDMac() == mac);

    AddAap("intf1", 1, Ip4Address(0), zero_mac.ToString());
    EXPECT_FALSE(RouteFind("vrf1", ip, 32));
    EXPECT_FALSE(EvpnRouteGet("vrf1", mac, ip, 0));
    EXPECT_TRUE(vm_intf->allowed_address_pair_list().list_.size() == 0);
}

//Just add a local path, verify that sequence no gets initialized to 0
TEST_F(TestAap, StateMachine_1) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));

    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", ip, 32);
    const AgentPath *path = rt->GetActivePath();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);
}

//Add a remote path with same preference and verify that local path
//moves to wait for traffic state
TEST_F(TestAap, StateMachine_2) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    Ip4Address server_ip = Ip4Address::from_string("10.1.1.3");

    PathPreference path_preference(1, PathPreference::LOW, false, false);
    TunnelType::TypeBmap bmap = (1 << TunnelType::MPLS_GRE);
    Inet4TunnelRouteAdd(peer_, "vrf1", ip, 32, server_ip, bmap,
                        16, "vn1", SecurityGroupList(), path_preference);
    client->WaitForIdle();

    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);
}

//Add a remote path with same preference and verify that local path
//moves to wait for traffic state
TEST_F(TestAap, StateMachine_3) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    Ip4Address server_ip = Ip4Address::from_string("10.1.1.3");

    PathPreference path_preference(1, PathPreference::LOW, false, false);
    TunnelType::TypeBmap bmap = (1 << TunnelType::MPLS_GRE);
    Inet4TunnelRouteAdd(peer_, "vrf1", ip, 32, server_ip, bmap,
                        16, "vn1", SecurityGroupList(), path_preference);
    client->WaitForIdle();

    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", ip, 32);
    EvpnRouteEntry *evpn_rt = EvpnRouteGet("vrf1",
                                           vm_intf->vm_mac(), ip, 0);
    const AgentPath *path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

    Agent::GetInstance()->oper_db()->route_preference_module()->
        EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                           vm_intf->vm_mac());
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 2);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    path = evpn_rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 1);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    evpn_rt = EvpnRouteGet("vrf1",
                            vm_intf->vm_mac(),
                            Ip4Address(0), 0);
    path = rt->FindPath(vm_intf->peer());
    path = evpn_rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 1);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);
}

//Verify that dependent static route gets high preference,
//when interface native IP sees traffic
TEST_F(TestAap, StateMachine_4) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    //Add a static route
   struct TestIp4Prefix static_route[] = {
       { Ip4Address::from_string("24.1.1.0"), 24},
       { Ip4Address::from_string("16.1.1.0"), 16},
   };

   AddInterfaceRouteTable("static_route", 1, static_route, 2);
   AddLink("virtual-machine-interface", "intf1",
           "interface-route-table", "static_route");
   Ip4Address sip = Ip4Address::from_string("24.1.1.0");
   client->WaitForIdle();

   VmInterface *vm_intf = VmInterfaceGet(1);
   InetUnicastRouteEntry *rt =
       RouteGet("vrf1", sip, 24);
   const AgentPath *path = rt->FindPath(vm_intf->peer());
   EXPECT_TRUE(path->path_preference().sequence() == 0);
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
   EXPECT_TRUE(path->path_preference().ecmp() == false);
   EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

   Agent::GetInstance()->oper_db()->route_preference_module()->
       EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                          vm_intf->vm_mac());
   client->WaitForIdle();
   EXPECT_TRUE(path->path_preference().sequence() == 0);
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
   EXPECT_TRUE(path->path_preference().ecmp() == false);
   EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);
}

//Upon transition of instance IP address from active-backup to active-active
//Verify that path preference becomes high when interface is in active-active
//mode
TEST_F(TestAap, StateMachine_5) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    AddActiveActiveInstanceIp("instance1", 1, "1.1.1.1");
    client->WaitForIdle();

    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == true);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    Agent::GetInstance()->oper_db()->route_preference_module()->
        EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                           vm_intf->vm_mac());
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == true);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);
}

//Upon transition of instance IP address from active-backup to active-active
//Verify that path preference of dependent static router, becomes high
//when interface is in active-active mode
TEST_F(TestAap, StateMachine_6) {
    //Add a static route
    struct TestIp4Prefix static_route[] = {
        { Ip4Address::from_string("24.1.1.0"), 24},
        { Ip4Address::from_string("16.1.1.0"), 16},
    };

    AddInterfaceRouteTable("static_route", 1, static_route, 2);
    AddLink("virtual-machine-interface", "intf1",
            "interface-route-table", "static_route");
    Ip4Address sip = Ip4Address::from_string("24.1.1.0");
    client->WaitForIdle();
    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", sip, 24);
    const AgentPath *path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

    AddActiveActiveInstanceIp("instance1", 1, "1.1.1.1");
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == true);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    AddInstanceIp("instance1", 1, "1.1.1.1");
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);
}

//Verify that a static route subnet route gets activated, when one of the
//ip in the subnet gets traffic
TEST_F(TestAap, StateMachine_7) {
    Ip4Address ip = Ip4Address::from_string("24.1.1.1");
    //Add a static route
    struct TestIp4Prefix static_route[] = {
        { Ip4Address::from_string("24.1.1.0"), 24},
        { Ip4Address::from_string("16.1.1.0"), 16},
    };

    AddInterfaceRouteTable("static_route", 1, static_route, 2);
    AddLink("virtual-machine-interface", "intf1",
            "interface-route-table", "static_route");
    Ip4Address sip = Ip4Address::from_string("24.1.1.0");
    client->WaitForIdle();
    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", sip, 24);
    const AgentPath *path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

    Agent::GetInstance()->oper_db()->route_preference_module()->
        EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                           vm_intf->vm_mac());
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

    Ip4Address native_ip = Ip4Address::from_string("1.1.1.1");
    Agent::GetInstance()->oper_db()->route_preference_module()->
        EnqueueTrafficSeen(native_ip, 32, vm_intf->id(),
                           vm_intf->vrf()->vrf_id(),
                           vm_intf->vm_mac());
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    rt = RouteGet("vrf1", native_ip, 32);
    path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 1);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);
}

//Create a interface with IP address 24.1.1.1(intf2)
//Fake traffic for that ip from intf1(static route 24.1.1.0/24)
//and verify that 24.1.1. doesnt get activated
TEST_F(TestAap, StateMachine_8) {
    struct PortInfo input1[] = {
        {"intf2", 2, "24.1.1.1", "00:00:00:01:01:01", 1, 2},
    };
    CreateVmportEnv(input1, 1);
    client->WaitForIdle();

    Ip4Address ip = Ip4Address::from_string("24.1.1.1");
    //Add a static route
    struct TestIp4Prefix static_route[] = {
        { Ip4Address::from_string("24.1.1.0"), 24},
        { Ip4Address::from_string("16.1.1.0"), 16},
    };

    AddInterfaceRouteTable("static_route", 1, static_route, 2);
    AddLink("virtual-machine-interface", "intf1",
            "interface-route-table", "static_route");
    Ip4Address sip = Ip4Address::from_string("24.1.1.0");
    client->WaitForIdle();
    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", sip, 24);
    const AgentPath *path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

    //Fake traffic on intf1 for 24.1.1.1
    Agent::GetInstance()->oper_db()->route_preference_module()->
        EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                           vm_intf->vm_mac());
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

    DeleteVmportEnv(input1, 1, false);
    client->WaitForIdle();
}

//Verify that dependent service route gets high preference,
//when interface native IP sees traffic
TEST_F(TestAap, StateMachine_9) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");

    AddVmPortVrf("ser1", "11.1.1.253", 1);
    AddLink("virtual-machine-interface-routing-instance", "ser1",
            "routing-instance", "vrf1");
    AddLink("virtual-machine-interface-routing-instance", "ser1",
            "virtual-machine-interface", "intf1");
   client->WaitForIdle();

   Ip4Address service_vlan_rt = Ip4Address::from_string("11.1.1.253");
   VmInterface *vm_intf = VmInterfaceGet(1);
   InetUnicastRouteEntry *rt =
       RouteGet("vrf1", service_vlan_rt, 32);
   const AgentPath *path = rt->FindPath(vm_intf->peer());
   EXPECT_TRUE(path->path_preference().sequence() == 0);
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
   EXPECT_TRUE(path->path_preference().ecmp() == false);
   EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

   Agent::GetInstance()->oper_db()->route_preference_module()->
       EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                          vm_intf->vm_mac());
   client->WaitForIdle();
   EXPECT_TRUE(path->path_preference().sequence() == 0);
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
   EXPECT_TRUE(path->path_preference().ecmp() == false);
   EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    Ip4Address server_ip = Ip4Address::from_string("10.1.1.1");
    TunnelType::TypeBmap bmap = (1 << TunnelType::MPLS_GRE);
    PathPreference path_preference(100, PathPreference::HIGH, false, false);
    Inet4TunnelRouteAdd(peer_, "vrf1", ip, 32, server_ip, bmap,
            16, "vn1", SecurityGroupList(), path_preference);
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
}

//Verify that static preference is populated
TEST_F(TestAap, StateMachine_10) {
    AddStaticPreference("intf1", 1, 200);
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));

    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", ip, 32);
    const AgentPath *path = rt->GetActivePath();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);
    EXPECT_TRUE(path->path_preference().static_preference() == true);
}

//Verify that preference value change is reflected with
//static preference change
TEST_F(TestAap, StaticMachine_11) {
    AddStaticPreference("intf1", 1, 100);
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));

    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", ip, 32);
    const AgentPath *path = rt->GetActivePath();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);
    EXPECT_TRUE(path->path_preference().static_preference() == true);

    AddStaticPreference("intf1", 1, 200);
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);
    EXPECT_TRUE(path->path_preference().static_preference() == true);

    // verify value other than HIGH and LOW
    AddStaticPreference("intf1", 1, 50);
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == 50);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);
    EXPECT_TRUE(path->path_preference().static_preference() == true);

    AddStaticPreference("intf1", 1, 100);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);

    //Delete static interface property
    AddNode("virtual-machine-interface", "intf1",
            1, "");
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().static_preference() == false);
}

//Verify that static preference is not populated
//when preference value is set to 0
TEST_F(TestAap, StateMachine_12) {
    AddStaticPreference("intf1", 1, 0);
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));

    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", ip, 32);
    const AgentPath *path = rt->GetActivePath();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);
    EXPECT_TRUE(path->path_preference().static_preference() == false);
}


//Check that agent retries to push the entry, if BGP doesnt update
//with last nexthop
TEST_F(TestAap, StateMachine_13) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    Ip4Address server_ip = Ip4Address::from_string("10.1.1.3");
    VmInterface *vm_intf = VmInterfaceGet(1);

    uint32_t seq_no = 1;
    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());

    for (uint32_t i = 0; i <= PathPreferenceSM::kMaxFlapCount; i++) {
        Agent::GetInstance()->oper_db()->route_preference_module()->
            EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                               vm_intf->vm_mac());
        client->WaitForIdle();
        const AgentPath *path = rt->FindPath(vm_intf->peer());
        EXPECT_TRUE(path->path_preference().sequence() == seq_no++);
        EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
        EXPECT_TRUE(path->path_preference().ecmp() == false);
        EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

        TunnelType::TypeBmap bmap = (1 << TunnelType::MPLS_GRE);
        PathPreference path_preference(seq_no++, PathPreference::HIGH, false, false);
        Inet4TunnelRouteAdd(peer_, "vrf1", ip, 32, server_ip, bmap,
                16, "vn1", SecurityGroupList(), path_preference);
        client->WaitForIdle();
        if (i != PathPreferenceSM::kMaxFlapCount) {
            EXPECT_TRUE(path->path_preference().preference() ==
                        PathPreference::LOW);
            EXPECT_TRUE(path->path_preference().ecmp() == false);
            EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);
        }
    }

    usleep(2 * 1000 * (PathPreferenceSM :: kMinInterval));
    //Check that agent withdraws its route after 100ms
    //since BGP path, didnt reflect local agent nexthop
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == true));
    client->WaitForIdle();
}

TEST_F(TestAap, Backoff_1) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());

    FlapRoute(ip, vm_intf, PathPreferenceSM::kMaxFlapCount);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    //Timeout has increase to 8 second
    usleep(2 * 1000 * (PathPreferenceSM::kMinInterval));
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == true));

    FlapRoute(ip, vm_intf, PathPreferenceSM::kMaxFlapCount);
    //Timeout increases to 16 sec
    usleep(2 * 1000 * (PathPreferenceSM::kMinInterval));
    //Path should be in wait for traffic after 8 second sleep
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == false));

    //16 second sleep over, path goes to wait for traffic state
    usleep(2 * 1000 * (PathPreferenceSM::kMinInterval));
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == true));
    client->WaitForIdle();
}

//Check that if route flap stops, timeout decreases
TEST_F(TestAap, Backoff_2) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());

    FlapRoute(ip, vm_intf, PathPreferenceSM::kMaxFlapCount);
    //Timeout is 8 seconds
    //Sleep for 16 seconds so that route flap stops
    usleep(4 * 1000 * (PathPreferenceSM::kMinInterval));
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == true));
    client->WaitForIdle();

    FlapRoute(ip, vm_intf, PathPreferenceSM::kMaxFlapCount);
    //Timeout : 8s
    //Sleep for : 8s
    usleep(2 * 1000 * (PathPreferenceSM::kMinInterval));
    //Check that agent withdraws its route after 100ms
    //since BGP path, didnt reflect local agent nexthop
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == true));
}

TEST_F(TestAap, Backoff_3) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt = RouteGet("vrf1", ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());

    //No continous flaps
    FlapRoute(ip, vm_intf, 4);
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == true));

    //Dummy sleep
    usleep(2 * 1000 * (PathPreferenceSM::kMinInterval));
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == true));
    client->WaitForIdle();
}

//Flap for 4 times, so that max back off is reached
//Sleep for 4 second of inactivity, verify path moves back to 4 second backoff
TEST_F(TestAap, Backoff_4) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());

    for (uint32_t i = 1; i <= 3; i++) {
        FlapRoute(ip, vm_intf, PathPreferenceSM::kMaxFlapCount);
        uint32_t backoff = 1 << i;
        usleep(backoff * 1000 * (PathPreferenceSM::kMinInterval));
        WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == true));
        client->WaitForIdle();
    }

    //No flap for 4s, hence backoff should be reset to 4s
    usleep((PathPreferenceSM::kMinInterval) * 1000);
    FlapRoute(ip, vm_intf, PathPreferenceSM::kMaxFlapCount);
    usleep(2 * 1000 * (PathPreferenceSM::kMinInterval));
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == true));
}

//Ensure backoff time continous at 32 seconds
//given route keeps on flapping after every backoff timer firing
TEST_F(TestAap, Backoff_5) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt =
        RouteGet("vrf1", ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());

    for (uint32_t i = 1; i <= 3; i++) {
        FlapRoute(ip, vm_intf, PathPreferenceSM::kMaxFlapCount);
        uint32_t backoff = 1 << i;
        usleep(backoff * 1000 * (PathPreferenceSM::kMinInterval));
        WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == true));
        client->WaitForIdle();
    }

    FlapRoute(ip, vm_intf, PathPreferenceSM::kMaxFlapCount);
    usleep(1000 * (PathPreferenceSM::kMinInterval));
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == false));
    usleep(2 * 1000 * (PathPreferenceSM::kMinInterval));
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == false));
    usleep(4 * 1000 * (PathPreferenceSM::kMinInterval));
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == false));
    usleep(1000 * (PathPreferenceSM::kMinInterval));
    WAIT_FOR(1000, 1000, (path->path_preference().wait_for_traffic() == true));
}

TEST_F(TestAap, StateMachine_16) {
    Ip4Address aap_ip = Ip4Address::from_string("10.10.10.10");
    AddEcmpAap("intf1", 1, aap_ip, MacAddress::ZeroMac().ToString());
    EXPECT_TRUE(RouteFind("vrf1", aap_ip, 32));

    VmInterface *vm_intf = VmInterfaceGet(1);
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    InetUnicastRouteEntry *rt = RouteGet("vrf1", aap_ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == true);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    Agent::GetInstance()->oper_db()->route_preference_module()->
        EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                           vm_intf->vm_mac());
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == true);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    Ip4Address server_ip = Ip4Address::from_string("10.1.1.1");
    TunnelType::TypeBmap bmap = (1 << TunnelType::MPLS_GRE);
    PathPreference path_preference(100, PathPreference::HIGH, false, false);
    Inet4TunnelRouteAdd(peer_, "vrf1", aap_ip, 32, server_ip, bmap,
            16, "vn1", SecurityGroupList(), path_preference);

    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == true);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    rt = RouteGet("vrf1", ip, 32);
    path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 1);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);
}

TEST_F(TestAap, StateMachine_17) {
    AddVn("default-project:vn2", 2);
    AddVrf("default-project:vn2:vn2", 2);
    AddLink("virtual-network", "default-project:vn2", "routing-instance",
            "default-project:vn2:vn2");
    client->WaitForIdle();

    //Configure Floating-IP for intf7 in default-project:vn1
    AddFloatingIpPool("fip-pool1", 1);
    AddFloatingIp("fip1", 1, "1.1.1.100");
    AddLink("floating-ip", "fip1", "floating-ip-pool", "fip-pool1");
    AddLink("floating-ip-pool", "fip-pool1", "virtual-network",
            "default-project:vn2");
    AddLink("virtual-machine-interface", "intf1", "floating-ip", "fip1");
    client->WaitForIdle();

    VmInterface *vm_intf = VmInterfaceGet(1);
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    Ip4Address fip = Ip4Address::from_string("1.1.1.100");
    InetUnicastRouteEntry *rt = RouteGet("default-project:vn2:vn2", fip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());

    EvpnRouteEntry *evpn_rt = EvpnRouteGet("default-project:vn2:vn2",
                                           vm_intf->vm_mac(), fip, 0);
    const AgentPath *evpn_path = evpn_rt->FindPath(vm_intf->peer());

    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(evpn_path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

    Agent::GetInstance()->oper_db()->route_preference_module()->
        EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                           vm_intf->vm_mac());
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(evpn_path->path_preference().preference() ==
                PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    Ip4Address server_ip = Ip4Address::from_string("10.1.1.1");
    TunnelType::TypeBmap bmap = (1 << TunnelType::MPLS_GRE);
    PathPreference path_preference(100, PathPreference::HIGH, false, false);
    Inet4TunnelRouteAdd(peer_, "vrf1", ip, 32, server_ip, bmap,
            16, "vn1", SecurityGroupList(), path_preference);

    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(evpn_path->path_preference().preference() ==
                PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    rt = RouteGet("vrf1", ip, 32);
    path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 1);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    DelLink("floating-ip", "fip1", "floating-ip-pool", "fip-pool1");
    DelLink("floating-ip-pool", "fip-pool1", "virtual-network",
            "default-project:vn2");
    DelLink("virtual-machine-interface", "intf1", "floating-ip", "fip1");
    DelLink("virtual-network", "default-project:vn2", "routing-instance",
            "default-project:vn2:vn2");
    DelVn("default-project:vn2");
    DelVrf("default-project:vn2:vn2");
    client->WaitForIdle();
}

//Upon interface deactivation and activation, make sure
//floating IP tracking happens
TEST_F(TestAap, StateMachine_18) {
    AddVn("default-project:vn2", 2);
    AddVrf("default-project:vn2:vn2", 2);
    AddLink("virtual-network", "default-project:vn2", "routing-instance",
            "default-project:vn2:vn2");
    client->WaitForIdle();

    VmInterface *vm_intf = VmInterfaceGet(1);
    //Configure Floating-IP for intf7 in default-project:vn1
    AddFloatingIpPool("fip-pool1", 1);
    AddFloatingIp("fip1", 1, "1.1.1.100");
    AddLink("floating-ip", "fip1", "floating-ip-pool", "fip-pool1");
    AddLink("floating-ip-pool", "fip-pool1", "virtual-network",
            "default-project:vn2");
    AddLink("virtual-machine-interface", "intf1", "floating-ip", "fip1");
    client->WaitForIdle();

    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    Ip4Address fip = Ip4Address::from_string("1.1.1.100");

    for (uint32_t i = 0; i < 100; i++) {
        DelLink("virtual-machine-interface", "intf1",
                "virtual-network", "vn1");
        client->WaitForIdle();
        AddLink("virtual-machine-interface", "intf1",
                "virtual-network", "vn1");
        client->WaitForIdle();
        InetUnicastRouteEntry *rt = RouteGet("default-project:vn2:vn2", fip, 32);
        const AgentPath *path = rt->FindPath(vm_intf->peer());

        EvpnRouteEntry *evpn_rt = EvpnRouteGet("default-project:vn2:vn2",
                                               vm_intf->vm_mac(), fip, 0);
        const AgentPath *evpn_path = evpn_rt->FindPath(vm_intf->peer());

        EXPECT_TRUE(path->path_preference().sequence() == 0);
        EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
        EXPECT_TRUE(evpn_path->path_preference().preference() ==
                    PathPreference::LOW);
        EXPECT_TRUE(path->path_preference().ecmp() == false);
        EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

        Agent::GetInstance()->oper_db()->route_preference_module()->
            EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                               vm_intf->vm_mac());
        client->WaitForIdle();
        EXPECT_TRUE(path->path_preference().sequence() == 0);
        EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
        EXPECT_TRUE(evpn_path->path_preference().preference() ==
                    PathPreference::HIGH);
        EXPECT_TRUE(path->path_preference().ecmp() == false);
        EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);
    }

    DelLink("floating-ip", "fip1", "floating-ip-pool", "fip-pool1");
    DelLink("floating-ip-pool", "fip-pool1", "virtual-network",
            "default-project:vn2");
    DelLink("virtual-machine-interface", "intf1", "floating-ip", "fip1");
    DelLink("virtual-network", "default-project:vn2", "routing-instance",
            "default-project:vn2:vn2");
    DelVn("default-project:vn2");
    DelVrf("default-project:vn2:vn2");
    client->WaitForIdle();
}

TEST_F(TestAap, StateMachine_19) {
    Ip4Address ip = Ip4Address::from_string("10.10.10.10");
    MacAddress mac("0a:0b:0c:0d:0e:0f");

    VmInterface *vm_intf = static_cast<VmInterface *>(VmPortGet(1));
    AddAap("intf1", 1, ip, mac.ToString());
    EXPECT_TRUE(RouteFind("vrf1", ip, 32));
    EXPECT_TRUE(EvpnRouteGet("vrf1", mac, ip, 0));
    EXPECT_TRUE(vm_intf->allowed_address_pair_list().list_.size() == 1);

    client->WaitForIdle();

    EvpnRouteEntry *rt = EvpnRouteGet("vrf1", mac, ip, 0);
    const AgentPath *path = rt->FindPath(vm_intf->peer());

    Agent::GetInstance()->oper_db()->route_preference_module()->
       EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                          mac);
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 1);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    AddSg("sg1", 1);
    AddAcl("acl1", 1);
    AddLink("security-group", "sg1", "access-control-list", "acl1");
    AddLink("virtual-machine-interface", "intf1", "security-group", "sg1");
    client->WaitForIdle();
    path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 1);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    DelLink("virtual-machine-interface", "vnet1", "security-group", "sg1");
    DelLink("security-group", "sg1", "access-control-list", "acl1");
    DelAcl("acl1");
    DelNode("security-group", "sg1");
    client->WaitForIdle();
}

TEST_F(TestAap, StateMachine_20) {
    Ip4Address ip = Ip4Address::from_string("1.1.1.1");

    AddVmPortVrf("ser1", "11.1.1.253", 1);
    AddLink("virtual-machine-interface-routing-instance", "ser1",
            "routing-instance", "vrf1");
    AddLink("virtual-machine-interface-routing-instance", "ser1",
            "virtual-machine-interface", "intf1");
   client->WaitForIdle();

   Ip4Address service_vlan_rt = Ip4Address::from_string("11.1.1.253");
   VmInterface *vm_intf = VmInterfaceGet(1);
   InetUnicastRouteEntry *rt =
       RouteGet("vrf1", service_vlan_rt, 32);
   const AgentPath *path = rt->FindPath(vm_intf->peer());
   EXPECT_TRUE(path->path_preference().sequence() == 0);
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
   EXPECT_TRUE(path->path_preference().ecmp() == false);
   EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

   Agent::GetInstance()->oper_db()->route_preference_module()->
       EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                          vm_intf->vm_mac());
   client->WaitForIdle();
   EXPECT_TRUE(path->path_preference().sequence() == 0);
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
   EXPECT_TRUE(path->path_preference().ecmp() == false);
   EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

   AddServiceInstanceIp("instaneip100", 100, "2.2.2.2", false, NULL);
   AddLink("virtual-machine-interface", "intf1", "instance-ip", "instaneip100");
   client->WaitForIdle();
   EXPECT_TRUE(path->path_preference().sequence() == 0);
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
   EXPECT_TRUE(path->path_preference().ecmp() == false);
   EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

   AddServiceInstanceIp("instaneip100", 100, "2.2.2.2", true, NULL);
   client->WaitForIdle();
   EXPECT_TRUE(path->path_preference().sequence() == 0);
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
   EXPECT_TRUE(path->path_preference().ecmp() == true);
   EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

   Ip6Address ip6 = Ip6Address::from_string("ffd2::11");
   Agent::GetInstance()->oper_db()->route_preference_module()->
       EnqueueTrafficSeen(ip6, 128, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                          vm_intf->vm_mac());
   client->WaitForIdle();
   EXPECT_TRUE(path->path_preference().sequence() == 0);
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
   EXPECT_TRUE(path->path_preference().ecmp() == true);
   EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);
}

//Change Aap mode from default to active-active and verify ecmp route exists
TEST_F(TestAap, AapModeChange) {
    Ip4Address aap_ip = Ip4Address::from_string("10.10.10.10");
    AddAap("intf1", 1, aap_ip, zero_mac.ToString());

    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt = RouteGet("vrf1", aap_ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

    Ip4Address ip = Ip4Address::from_string("1.1.1.1");
    AddEcmpAap("intf1", 1, aap_ip, MacAddress::ZeroMac().ToString());
    EXPECT_TRUE(RouteFind("vrf1", aap_ip, 32));
    rt = RouteGet("vrf1", aap_ip, 32);
    path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == true);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

    Agent::GetInstance()->oper_db()->route_preference_module()->
        EnqueueTrafficSeen(ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                vm_intf->vm_mac());
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == true);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);


    //Change back to active-backup and verify preference
    AddAap("intf1", 1, aap_ip, zero_mac.ToString());
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path->path_preference().ecmp() == false);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

    //Change back to active-active mode and verify AAP is in high
    //preference since native-ip is already in high preference
    AddEcmpAap("intf1", 1, aap_ip, MacAddress::ZeroMac().ToString());
    EXPECT_TRUE(path->path_preference().sequence() == 0);
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
    EXPECT_TRUE(path->path_preference().ecmp() == true);
    EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);
}

TEST_F(TestAap, ServiceIpTrackingIp_1) {
    Ip4Address aap_ip = Ip4Address::from_string("10.10.10.10");
    Ip4Address aap_ip1 = Ip4Address::from_string("10.10.10.11");
    Ip4Address service_ip = Ip4Address::from_string("2.2.2.2");
    AddAap("intf1", 1, aap_ip, zero_mac.ToString());

    AddServiceInstanceIp("instanceip100", 100, "2.2.2.2", false,
                         "10.10.10.10");
    AddLink("virtual-machine-interface", "intf1",
            "instance-ip", "instanceip100");
    client->WaitForIdle();

    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt = RouteGet("vrf1", service_ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());

    EvpnRouteEntry *evpn_rt = EvpnRouteGet("vrf1",
                                           vm_intf->vm_mac(), service_ip, 0);
    const AgentPath *evpn_path = evpn_rt->FindPath(vm_intf->peer());

    Agent::GetInstance()->oper_db()->route_preference_module()->
       EnqueueTrafficSeen(aap_ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                          vm_intf->vm_mac());
   client->WaitForIdle();

   EXPECT_TRUE(path->path_preference().sequence() == 0);
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
   EXPECT_TRUE(path->path_preference().ecmp() == false);
   EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

   EXPECT_TRUE(evpn_path->path_preference().sequence() == 0);
   EXPECT_TRUE(evpn_path->path_preference().preference() == PathPreference::HIGH);
   EXPECT_TRUE(evpn_path->path_preference().ecmp() == false);
   EXPECT_TRUE(evpn_path->path_preference().wait_for_traffic() == false);

   //Change the tracking ip, and verify previous tracking ip is deleted
   AddAap("intf1", 1, aap_ip1, zero_mac.ToString());
   EXPECT_TRUE(path->path_preference().sequence() == 0);
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
   EXPECT_TRUE(path->path_preference().ecmp() == false);
   EXPECT_TRUE(path->path_preference().wait_for_traffic() == true);

   EXPECT_TRUE(evpn_path->path_preference().sequence() == 0);
   EXPECT_TRUE(evpn_path->path_preference().preference() == PathPreference::LOW);
   EXPECT_TRUE(evpn_path->path_preference().ecmp() == false);
   EXPECT_TRUE(evpn_path->path_preference().wait_for_traffic() == true);
}

TEST_F(TestAap, ServiceIpTrackingIp_2) {
    Ip4Address aap_ip = Ip4Address::from_string("10.10.10.10");
    Ip4Address aap_ip1 = Ip4Address::from_string("10.10.10.11");
    Ip4Address service_ip = Ip4Address::from_string("2.2.2.2");
    AddAap("intf1", 1, aap_ip, zero_mac.ToString());

    AddServiceInstanceIp("instanceip100", 100, "2.2.2.2", false,
                         "10.10.10.10");
    AddLink("virtual-machine-interface", "intf1",
            "instance-ip", "instanceip100");
    client->WaitForIdle();

    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt = RouteGet("vrf1", service_ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());

    EvpnRouteEntry *evpn_rt = EvpnRouteGet("vrf1",
                                           vm_intf->vm_mac(), service_ip, 0);
    const AgentPath *evpn_path = evpn_rt->FindPath(vm_intf->peer());

    Agent::GetInstance()->oper_db()->route_preference_module()->
       EnqueueTrafficSeen(aap_ip, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
                          vm_intf->vm_mac());
   client->WaitForIdle();

   EXPECT_TRUE(path->path_preference().sequence() == 0);
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
   EXPECT_TRUE(path->path_preference().ecmp() == false);
   EXPECT_TRUE(path->path_preference().wait_for_traffic() == false);

   EXPECT_TRUE(evpn_path->path_preference().sequence() == 0);
   EXPECT_TRUE(evpn_path->path_preference().preference() == PathPreference::HIGH);
   EXPECT_TRUE(evpn_path->path_preference().ecmp() == false);
   EXPECT_TRUE(evpn_path->path_preference().wait_for_traffic() == false);

   AddServiceInstanceIp("instanceip100", 100, "2.2.2.2", false,
                         "10.10.10.11");
   client->WaitForIdle();

   evpn_rt = EvpnRouteGet("vrf1", vm_intf->vm_mac(), service_ip, 0);
   evpn_path = evpn_rt->FindPath(vm_intf->peer());

   EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
   EXPECT_TRUE(evpn_path->path_preference().preference() == PathPreference::LOW);

   //Change the tracking ip, and verify previous tracking ip is deleted
   AddAap("intf1", 1, aap_ip1, zero_mac.ToString());
   Agent::GetInstance()->oper_db()->route_preference_module()->
       EnqueueTrafficSeen(aap_ip1, 32, vm_intf->id(), vm_intf->vrf()->vrf_id(),
               vm_intf->vm_mac());
   client->WaitForIdle();

   evpn_path = evpn_rt->FindPath(vm_intf->peer());
   EXPECT_TRUE(path->path_preference().preference() == PathPreference::HIGH);
   EXPECT_TRUE(evpn_path->path_preference().preference() == PathPreference::HIGH);
}

TEST_F(TestAap, ServiceIpTrackingIp_3) {
    AddServiceInstanceIp("instanceip100", 100, "2.2.2.2", false,
                         "2.2.2.2");
    AddLink("virtual-machine-interface", "intf1",
            "instance-ip", "instanceip100");
    client->WaitForIdle();

    Ip4Address service_ip = Ip4Address::from_string("2.2.2.2");

    VmInterface *vm_intf = VmInterfaceGet(1);
    InetUnicastRouteEntry *rt = RouteGet("vrf1", service_ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().dependent_ip() == Ip4Address(0));

    EvpnRouteEntry *evpn_rt = EvpnRouteGet("vrf1",
                                           vm_intf->vm_mac(), service_ip, 0);
    const AgentPath *evpn_path = evpn_rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(evpn_path->path_preference().dependent_ip() == Ip4Address(0));
}

TEST_F(TestAap, ServiceIpTrackingIp_4) {
    Ip4Address aap_ip = Ip4Address::from_string("2.2.2.2");
    Ip4Address tracking_ip = Ip4Address::from_string("10.10.10.10");

    AddServiceInstanceIp("instanceip100", 100, "2.2.2.2", false,
                         "10.10.10.10");
    AddLink("virtual-machine-interface", "intf1",
            "instance-ip", "instanceip100");
    client->WaitForIdle();

    AddAap("intf2", 2, aap_ip, zero_mac.ToString());
    client->WaitForIdle();

    Ip4Address service_ip = Ip4Address::from_string("2.2.2.2");

    VmInterface *vm_intf = VmInterfaceGet(1);
    VmInterface *vm_intf2 = VmInterfaceGet(2);
    InetUnicastRouteEntry *rt = RouteGet("vrf1", service_ip, 32);
    const AgentPath *path = rt->FindPath(vm_intf->peer());
    EXPECT_TRUE(path->path_preference().dependent_ip() == tracking_ip);
    const AgentPath *path2 = rt->FindPath(vm_intf2->peer());
    EXPECT_TRUE(path2->path_preference().dependent_ip() == Ip4Address(0));

    Agent::GetInstance()->oper_db()->route_preference_module()->
        EnqueueTrafficSeen(aap_ip, 32, vm_intf2->id(), vm_intf2->vrf()->vrf_id(),
                vm_intf2->vm_mac());
    client->WaitForIdle();
    EXPECT_TRUE(path->path_preference().preference() == PathPreference::LOW);
    EXPECT_TRUE(path2->path_preference().preference() == PathPreference::HIGH);

}

int main(int argc, char *argv[]) {
    GETUSERARGS();
    client = TestInit(init_file, ksync_init);
    int ret = RUN_ALL_TESTS();
    TestShutdown();
    return ret;
}
