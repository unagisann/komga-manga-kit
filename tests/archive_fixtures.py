"""Original tiny stored RAR fixtures, built from headers without third-party books."""
import struct
import zlib


def stored_rar(path, members, version=4):
    if version == 4:
        def header(body):
            return struct.pack('<H', zlib.crc32(body) & 0xffff) + body
        content = bytearray(b'Rar!\x1a\x07\x00' + header(struct.pack('<BHHHI', 0x73, 0, 13, 0, 0)))
        for filename, data in members:
            name = filename.encode('ascii')
            body = struct.pack('<BHHIIBIIBBHI', 0x74, 0x8000, 32 + len(name), len(data), len(data),
                               3, zlib.crc32(data), 0, 20, 0x30, len(name), 0o100644) + name
            content.extend(header(body) + data)
        content.extend(header(struct.pack('<BHH', 0x7b, 0, 7)))
    else:
        def vint(value):
            result = bytearray()
            while value >= 128:
                result.append((value & 127) | 128)
                value >>= 7
            result.append(value)
            return bytes(result)
        def header(body):
            encoded = vint(len(body)) + body
            return struct.pack('<I', zlib.crc32(encoded)) + encoded
        content = bytearray(b'Rar!\x1a\x07\x01\x00' + header(b'\x01\x00\x00'))
        for filename, data in members:
            name = filename.encode('utf-8')
            body = b'\x02\x02' + vint(len(data)) + b'\x04' + vint(len(data)) + vint(0o100644)
            body += struct.pack('<I', zlib.crc32(data)) + b'\x00\x01' + vint(len(name)) + name
            content.extend(header(body) + data)
        content.extend(header(b'\x05\x00\x00'))
    path.write_bytes(content)
