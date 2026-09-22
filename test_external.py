import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from external_utils import valid_png, inspect_frames
from external import script_info

def png():
    def chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+
            chunk(b'IDAT',zlib.compress(b'\x00\xff\x00\x00'))+chunk(b'IEND',b''))

class ExternalTests(unittest.TestCase):
    def test_only_complete_crc_valid_images_count(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'0001.png'
            job={'output':folder,'start':1,'end':2,'step':1,'external':{'pattern':'####.png'}}
            cache={}
            path.write_bytes(png()[:-4])
            self.assertFalse(valid_png(path))
            self.assertEqual(inspect_frames(job,cache),{})
            path.write_bytes(png())
            self.assertTrue(valid_png(path))
            self.assertEqual(set(inspect_frames(job,cache)),{'1'})
            broken=bytearray(png());broken[45]^=1
            path.write_bytes(broken)
            self.assertFalse(valid_png(path))
            self.assertEqual(inspect_frames(job,cache),{})

    def test_gui_and_animation_commands_cannot_attach(self):
        for args in [['blender.exe','scene.blend','--python','script.py'],
                     ['blender.exe','-b','scene.blend','--python','script.py','-a']]:
            with self.assertRaises(ValueError): script_info({'args':args,'blend':'scene.blend'})

if __name__=='__main__': unittest.main()
