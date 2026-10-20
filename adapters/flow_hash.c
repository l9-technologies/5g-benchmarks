// SPDX-License-Identifier: GPL-2.0
// Set a stable per-UE receive hash; do not change packet bytes.
#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>
#ifndef RECEIVE_CPUS
#error "Set RECEIVE_CPUS to the peer receive allocation"
#endif
SEC("classifier")
int flow_hash(struct __sk_buff *skb)
{
    unsigned char *data=(void *)(long)skb->data, *end=(void *)(long)skb->data_end;
    if (data+34>end || data[12]!=8 || data[13]!=0 || data[14]!=0x45) return 0;
    unsigned int offset=14;
    // shortcut: this profile has no IP options and at most one four-byte PDU container; extend for other profiles.
    if (data[23]==17) {
        if (data+38>end) return 0;
        if (data[36]==8 && data[37]==104) {
            if (data+50>end || data[43]!=255 || (data[42]>>5)!=1) return 0;
            offset=50;
            if (data[42]&7) {
                if (data+54>end) return 0;
                offset=54;
            }
            if (data[42]&4) {
                if (data+58>end || data[53]!=0x85 || data[54]!=1 || data[57]!=0) return 0;
                offset=58;
            }
        }
    }
    struct iphdr *ip=(void *)(data+offset);
    if ((void *)(ip+1)>(void *)end || ip->version!=4 || ip->ihl!=5) return 0;
    unsigned int source=bpf_ntohl(ip->saddr), destination=bpf_ntohl(ip->daddr), ue;
    if ((source&0xffffff00)==0x0a2d0000) ue=source;
    else if ((destination&0xffffff00)==0x0a2d0000) ue=destination;
    else {
        // NAT hides the UE address; the declared DN server port still names its flow.
        unsigned char *ports=(void *)(ip+1);
        if (ports+4>end || (ip->protocol!=6 && ip->protocol!=17)) return 0;
        unsigned int port;
        if (destination==0xc0000201) port=((unsigned int)ports[2]<<8)|ports[3];
        else if (source==0xc0000201) port=((unsigned int)ports[0]<<8)|ports[1];
        else return 0;
        if (port<5201 || port>5400) return 0;
        ue=port-5200;
    }
    unsigned int bucket=ue%RECEIVE_CPUS;
    bpf_set_hash(skb,((unsigned long long)(2*bucket+1)*0x80000000ULL)/RECEIVE_CPUS);
#ifdef TEST_STEERING
    skb->cb[0]=skb->hash;
#endif
    return 0;
}
char LICENSE[] SEC("license")="GPL";
