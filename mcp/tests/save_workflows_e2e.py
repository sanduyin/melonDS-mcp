"""Real MCP stdio save/table workflow using an original EEPROM test cartridge.

No commercial ROM, BIOS or personal save is used. This exercises persistence of
cartridge bytes, not a game's save menu; see the separately documented game case.
SPDX-License-Identifier: GPL-3.0-or-later
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from synthetic_rom import build_rom, _crc16


def save_rom(path):
    data = bytearray(build_rom())
    # Use the ordinary EEPROM cartridge path, with original code outside the
    # secure area. No Nintendo logo, retail program or firmware is included.
    data[12:16] = b'ZMCP'
    for old, new, field in [(0x200,0x8000,0x20),(0x400,0x8200,0x30)]:
        data[new:new+0x200] = data[old:old+0x200]
        struct.pack_into('<I',data,field,new)
    struct.pack_into('<H',data,0x15E,_crc16(data[:0x15E]))
    path.write_bytes(data)
    return path


async def workflow(library, artifacts):
    root=Path(__file__).resolve().parents[2]
    env=dict(os.environ, PYTHONPATH=str(root/'mcp/python'),MELONDS_MCP_LIB=str(library.resolve()))
    env.pop('MELONDS_MCP_ROM',None)
    params=StdioServerParameters(command=sys.executable,args=['-B','-m','melonds_mcp'],env=env,cwd=str(root))
    calls=[]
    async def call(session,name,**args):
        result=await session.call_tool(name,args)
        assert not result.isError,(name,result.content)
        data=result.structuredContent
        if data is None:
            data=json.loads('\n'.join(item.text for item in result.content if getattr(item,'type',None)=='text'))
        assert data.get('ok',True),(name,data)
        calls.append({'tool':name,'outcome':'ok'})
        return data
    async def reject(session,name,**args):
        result=await session.call_tool(name,args)
        assert result.isError,(name,result)
        calls.append({'tool':name,'outcome':'rejected'})

    with tempfile.TemporaryDirectory(prefix='melonds-save-e2e-') as directory:
        temp=Path(directory)
        rom=save_rom(temp/'original.nds')
        seed=bytes(range(256))*32
        source=temp/'original.sav';source.write_bytes(seed)
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                prepared=await call(session,'save_workspace_prepare',rom_path=str(rom),save_path=str(source),workspace_dir=str(temp/'isolated'))
                await reject(session,'save_workspace_prepare',rom_path=str(rom),save_path=str(source),workspace_dir=str(temp/'isolated'))
                await call(session,'load_rom',path=prepared['rom_path'])
                await call(session,'advance_frames',frames=2)
                base=0x02010000
                # A multi-chunk table: only 0->1; preserve an existing owned=2.
                # Also cover the legacy bus-write API and preserve its gap byte.
                await call(session,'write_memory_bytes',address=base+1,hex_data='55',cpu=0)
                await call(session,'memory_poke',address=base,hex_data='02')
                table=await call(session,'memory_table_read',address=base,stride=88,count=194)
                assert table['values']==[2]+[0]*193,table['counts']
                edited=await call(session,'memory_table_patch',address=base,stride=88,count=194,value=1,replace_values=[0],expected_sha256=table['sha256'])
                assert edited['changed_count']==193 and edited['frame_number']==table['frame_number']
                await reject(session,'memory_table_patch',address=base,stride=88,count=194,value=1,replace_values=[0],expected_sha256=table['sha256'])
                after=await call(session,'memory_table_read',address=base,stride=88,count=194)
                assert after['values']==[2]+[1]*193
                gap=await call(session,'memory_peek',address=base,length=2)
                assert gap['hex']=='0255'
                scanned=await call(session,'memory_scan',address=base,span=88*193+1,pattern_hex='02')
                assert scanned['addresses']==[base] and not scanned['truncated']
                code=await call(session,'cheat_generate_ar',address=base,value=1,width=1,count=194,stride=88)
                assert 'C0000000 000000C1' in code['code'] and not code['enabled']
                exported=await call(session,'backup_export',path=str(temp/'exported.sav'))
                assert exported['sha256']==hashlib.sha256(seed).hexdigest()
                assert (temp/'exported.sav').read_bytes()==seed
                await reject(session,'backup_export',path=str(temp/'exported.sav'))
                assert source.read_bytes()==seed
        # A genuinely new stdio process/core: no savestate load or reset shortcut.
        cold_rom=save_rom(temp/'cold.nds')
        (temp/'cold.sav').write_bytes((temp/'exported.sav').read_bytes())
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                await call(session,'load_rom',path=str(cold_rom))
                cold=await call(session,'backup_export',path=str(temp/'cold-export.sav'))
                assert cold['sha256']==exported['sha256']
        result={'backend':'real_native_dll_via_mcp_stdio','calls':len(calls),
                'verified_tools':sorted({c['tool'] for c in calls}),
                'call_outcomes':{'ok':sum(c['outcome']=='ok' for c in calls),'rejected_as_expected':sum(c['outcome']=='rejected' for c in calls)},
                'evidence':{'preserved_owned_flag':True,'multi_chunk_patch':True,'stale_hash_rejected':True,
                            'source_unchanged':True,'battery_export_cold_roundtrip':True,
                            'ram_patch_did_not_silently_save':True,'ar_execution_tested':False},
                'rom':'original synthetic EEPROM cartridge; no commercial data'}
        artifacts.mkdir(parents=True,exist_ok=True)
        (artifacts/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library',type=Path,required=True)
    parser.add_argument('--artifacts',type=Path,default=Path('build/save-workflows-e2e'))
    args=parser.parse_args()
    print(json.dumps(asyncio.run(asyncio.wait_for(workflow(args.library,args.artifacts),timeout=90)),indent=2))
