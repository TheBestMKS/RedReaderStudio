"""Create multipart ZIP64 portable distribution without third-party archivers."""
from pathlib import Path
import hashlib,io,json,zipfile,sys,shutil

class Parts(io.RawIOBase):
    def __init__(self,folder,prefix,limit=1900*1024**2):
        self.folder=folder;self.prefix=prefix;self.limit=limit;self.total=0;self.part=None;self.size=0;self.entries=[];self.digest=None
    def writable(self):return True
    def seekable(self):return False
    def tell(self):return self.total
    def write(self,data):
        total=len(data);view=memoryview(data)
        while view:
            if self.part is None or self.size==self.limit:
                self.finish_part();name=f'{self.prefix}.part{len(self.entries)+1:03d}'
                self.part=(self.folder/name).open('wb');self.size=0;self.digest=hashlib.sha256();self.name=name
            chunk=view[:self.limit-self.size];self.part.write(chunk);self.digest.update(chunk)
            self.size+=len(chunk);self.total+=len(chunk);view=view[len(chunk):]
        return total
    def finish_part(self):
        if self.part is not None:
            self.part.close();self.entries.append(dict(name=self.name,size=self.size,sha256=self.digest.hexdigest()));self.part=None
            print(self.entries[-1]['name'],flush=True)
    def close(self):
        self.finish_part();super().close()

if __name__=='__main__':
    source=Path(sys.argv[1]).resolve();output=Path(sys.argv[2]).resolve();output.mkdir(parents=True,exist_ok=True)
    prefix='RedReaderStudio-'+source.name+'-Windows-x64.zip'
    if list(output.glob(prefix+'.part*')):raise SystemExit('Destination already has parts; use an empty output directory.')
    stream=Parts(output,prefix)
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as archive:
        for p in sorted(source.rglob('*')):
            relative=p.relative_to(source)
            if p.is_file() and not any(v in relative.parts for v in ['projects','voices','logs','cache','api-data','__pycache__','.pytest_cache','.cache']):
                archive.write(p,'RedReaderStudio-'+source.name+'/'+relative.as_posix())
    stream.close()
    (output/'portable-parts.json').write_text(json.dumps(dict(archive=prefix,folder='RedReaderStudio-'+source.name,parts=stream.entries),indent=2),encoding='utf-8')
    shutil.copy2(Path(__file__).with_name('Extract-RedReaderStudio.ps1'),output/'Extract-RedReaderStudio.ps1')
    print(f'Packaged {stream.total} bytes in {len(stream.entries)} parts',flush=True)
