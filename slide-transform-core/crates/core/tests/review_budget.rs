#![cfg(all(feature = "fixtures", feature = "jp2k"))]
#[cfg(test)] mod tests {
use slide_transform_core::{convert_svs,io::{MemSource,MemSink,MemScratch},plan::{TransformPlan,InputIdentity},svs_fixture::{SvsJp2kFixture,build_synthetic_svs_jp2k},error::ErrorCode};
fn fixture(n: &str) -> Vec<u8> {std::fs::read(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/jp2k").join(n)).unwrap()}
#[test] fn jp2k_respects_the_host_memory_budget() {
let t=fixture("cc0-33005-l0.j2k"); let l1=fixture("cc0-33005-l1.j2k");
let f=SvsJp2kFixture{compression:33005,photometric:2,tile:240,width:480,height:480,downsample:4,mpp:Some(0.499),appmag:Some(20.),levels:vec![vec![t.clone(),t.clone(),t.clone(),t],vec![l1.clone()],vec![l1.clone()],vec![l1]]};
let mut input=MemSink::new();build_synthetic_svs_jp2k(&mut input,&f).unwrap();
let src=MemSource::new(input.data);let mut out=MemSink::new();let mut scratch=MemScratch::default();
let mut p=TransformPlan::brightfield(InputIdentity::default());p.limits.memory_budget_bytes=1;
let result=convert_svs::convert_svs(&src,&mut out,&mut scratch,&p);
assert!(matches!(result,Err(ref e) if e.code==ErrorCode::ResourceLimitExceeded),"expected pre-allocation refusal, got {result:?}; {} output bytes",out.data.len());
}
}
