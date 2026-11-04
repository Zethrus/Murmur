use std::{
    net::{IpAddr, SocketAddr},
    sync::atomic::{AtomicU64, Ordering},
};

/// Accept only local address space. Check mapped IPv4 as IPv4, so mapped public
/// addresses cannot bypass the IPv6 policy.
pub fn allows(peer: SocketAddr) -> bool {
    match peer.ip() {
        IpAddr::V4(ip) => {
            let [a, b, ..] = ip.octets();
            a == 10
                || a == 127
                || (a == 172 && (16..=31).contains(&b))
                || (a == 192 && b == 168)
                || (a == 169 && b == 254)
        }
        IpAddr::V6(ip) => {
            if let Some(v4) = ip.to_ipv4_mapped() {
                return allows(SocketAddr::new(v4.into(), peer.port()));
            }
            let first = ip.octets();
            ip.is_loopback()
                || (first[0] & 0xfe == 0xfc)
                || (first[0] == 0xfe && first[1] & 0xc0 == 0x80)
        }
    }
}
pub fn admit(peer: SocketAddr, rejected: &AtomicU64) -> bool {
    if allows(peer) {
        return true;
    }
    rejected.fetch_add(1, Ordering::Relaxed);
    tracing::warn!(address = %peer.ip(), "Refused non-LAN companion peer");
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn private_and_public_matrix() {
        for (address, allowed) in [
            ("10.0.0.1:1", true),
            ("127.0.0.1:1", true),
            ("172.16.0.1:1", true),
            ("172.31.255.255:1", true),
            ("172.15.255.255:1", false),
            ("172.32.0.1:1", false),
            ("192.168.1.1:1", true),
            ("169.254.1.1:1", true),
            ("8.8.8.8:1", false),
            ("100.64.0.1:1", false),
            ("[::1]:1", true),
            ("[fc00::1]:1", true),
            ("[fdff::1]:1", true),
            ("[fe80::1]:1", true),
            ("[febf::1]:1", true),
            ("[fec0::1]:1", false),
            ("[2001:db8::1]:1", false),
            ("[::ffff:192.168.1.2]:1", true),
            ("[::ffff:8.8.8.8]:1", false),
        ] {
            assert_eq!(allows(address.parse().unwrap()), allowed, "{address}");
        }
    }
    #[test]
    fn refusal_is_counted() {
        let count = AtomicU64::new(0);
        assert!(!admit("8.8.8.8:1".parse().unwrap(), &count));
        assert!(admit("127.0.0.1:1".parse().unwrap(), &count));
        assert_eq!(count.load(Ordering::Relaxed), 1);
    }
}
