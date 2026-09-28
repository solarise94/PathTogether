use std::time::Instant;
fn main() {
    let dir = std::env::args().nth(1).unwrap();
    let iters: u64 = std::env::args().nth(2).unwrap().parse().unwrap();
    let mut seeds = vec![];
    for e in std::fs::read_dir(&dir).unwrap() { seeds.push(std::fs::read(e.unwrap().path()).unwrap()); }
    let mut s: u64 = 0x2545F4914F6CDD1D;
    let mut rnd = move || { s ^= s << 13; s ^= s >> 7; s ^= s << 17; s };
    std::panic::set_hook(Box::new(|_| {}));
    let (mut ok, mut err, mut panics, mut slow) = (0u64, 0u64, 0u64, 0u64);
    let mut worst = 0u128;
    for i in 0..iters {
        let mut d = seeds[(rnd() as usize) % seeds.len()].clone();
        let n = 1 + rnd() % 8;
        for _ in 0..n {
            if d.is_empty() { break; }
            let p = (rnd() as usize) % d.len();
            match rnd() % 6 {
                0 => d[p] ^= 1 << (rnd() % 8),
                1 => d[p] = rnd() as u8,
                2 => d.truncate(p),
                3 => { let v = [0xFFu8, (0xC0 + rnd() % 0x3F) as u8]; d.splice(p..p, v); }
                4 => { let q = (rnd() as usize) % d.len(); d[p] = d[q]; }
                _ => { if p + 1 < d.len() { d[p] = 0xFF; d[p+1] = 0xFF; } }
            }
        }
        let t = Instant::now();
        let r = std::panic::catch_unwind(|| {
            let _ = slide_transform_core::jpeg::decoder::scan_jpeg(&d);
            let _ = slide_transform_core::jpeg::decoder::extract_qtables(&d);
            let _ = slide_transform_core::jpeg::qtables_pillow_style(&d);
            slide_transform_core::jpeg::decoder::decode(&d, 1024 * 1024).is_ok()
        });
        let el = t.elapsed().as_micros();
        if el > worst { worst = el; }
        if el > 500_000 { slow += 1; std::fs::write(format!("{}/../slow-{}.jpg", dir, i), &d).ok(); }
        match r { Ok(true) => ok += 1, Ok(false) => err += 1, Err(_) => { panics += 1; std::fs::write(format!("{}/../panic-{}.jpg", dir, i), &d).ok(); } }
    }
    println!("iters={} ok={} typed_err={} panics={} slow(>0.5s)={} worst_us={}", iters, ok, err, panics, slow, worst);
}
