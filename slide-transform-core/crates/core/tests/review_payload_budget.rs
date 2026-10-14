#![cfg(all(feature = "fixtures", feature = "jp2k"))]
#[cfg(test)] mod tests {
use slide_transform_core::{convert_svs,io::{ByteSource,MemSink,MemScratch},plan::{TransformPlan,InputIdentity},svs_fixture::{SvsJp2kFixture,build_synthetic_svs_jp2k},error::{CoreError,CoreResult,ErrorCode}};
use std::cell::Cell;
fn fixture(n:&str)->Vec<u8>{std::fs::read(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/jp2k").join(n)).unwrap()}
struct SparseSource {data:Vec<u8>,large_reads:Cell<usize>}
impl ByteSource for SparseSource {
fn size(&self)->u64{512*1024*1024}
fn read_at(&self,off:u64,len:usize)->CoreResult<Vec<u8>>{
if len>1024*1024 {self.large_reads.set(self.large_reads.get()+1);return Err(CoreError::io("test blocks allocation above 1MiB"));}
let mut result=vec![0;len];let start=off as usize;
if start<self.data.len(){let n=len.min(self.data.len()-start);result[..n].copy_from_slice(&self.data[start..start+n]);}Ok(result)
}}
#[test] fn compressed_payload_is_admitted_before_large_read(){
let t=fixture("cc0-33005-l0.j2k");let l=fixture("cc0-33005-l1.j2k");
let f=SvsJp2kFixture{compression:33005,photometric:2,tile:240,width:480,height:480,downsample:4,mpp:Some(0.499),appmag:Some(20.),levels:vec![vec![t.clone(),t.clone(),t.clone(),t],vec![l.clone()],vec![l.clone()],vec![l]]};
let mut input=MemSink::new();build_synthetic_svs_jp2k(&mut input,&f).unwrap();let mut bytes=input.data;
let ifd=u32::from_le_bytes(bytes[4..8].try_into().unwrap()) as usize;
let count=u16::from_le_bytes(bytes[ifd..ifd+2].try_into().unwrap()) as usize;let mut patched=false;
for i in 0..count {let e=ifd+2+12*i;if u16::from_le_bytes(bytes[e..e+2].try_into().unwrap())==325 {assert_eq!(u16::from_le_bytes(bytes[e+2..e+4].try_into().unwrap()),4);let a=u32::from_le_bytes(bytes[e+8..e+12].try_into().unwrap()) as usize;bytes[a..a+4].copy_from_slice(&(256u32*1024*1024).to_le_bytes());patched=true;}}
assert!(patched);
let src=SparseSource{data:bytes,large_reads:Cell::new(0)};let mut out=MemSink::new();let mut scratch=MemScratch::default();let plan=TransformPlan::brightfield(InputIdentity::default());
let result=convert_svs::convert_svs(&src,&mut out,&mut scratch,&plan);
assert_eq!(src.large_reads.get(),0,"192MiB saver budget must reject the 256MiB compressed tile before read_at: {result:?}");
assert!(matches!(result,Err(ref e) if e.code==ErrorCode::ResourceLimitExceeded));
}
}
