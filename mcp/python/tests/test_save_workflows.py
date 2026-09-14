"""Behavioral contracts for isolated saves, guarded edits and AR generation."""
import asyncio
import hashlib
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from melonds_mcp import tools_save
from melonds_mcp.tool_boundary import ToolBoundary

BASE = 0x02000000


@pytest.fixture
def client():
    data = bytearray(131072)
    writes = []
    def poke(cpu, address, payload, instruction=False):
        writes.append((address, payload))
        data[address-BASE:address-BASE+len(payload)] = payload
        return len(payload)
    def export(path):
        Path(path.decode()).write_bytes(b'\x5a'*512)
        return 1
    native = SimpleNamespace(melonds_backup_export=export)
    bridge = SimpleNamespace(lib=native, peek_block=lambda cpu,a,n: bytes(data[a-BASE:a-BASE+n]),
                             poke_block=poke, get_status=lambda: [1, 77])
    emu = SimpleNamespace(lib=bridge, rom_path='isolated/game.nds', lock=threading.RLock(), ensure_init=lambda: None)
    server = FastMCP('save-test')
    tools_save.register(ToolBoundary(server, emu), emu)
    def call(name, **kwargs):
        return asyncio.run(server._tool_manager.get_tool(name).run(kwargs))
    return call, data, writes, emu


def test_patch_preserves_owned_flags_neighbors_and_frame(client):
    call, data, writes, _ = client
    data[:12] = bytes([0,90,90,90,1,91,91,91,2,92,92,92])
    snapshot = call('memory_table_read', address=BASE,stride=4,count=3)
    assert snapshot['values'] == [0,1,2]
    patched = call('memory_table_patch',address=BASE,stride=4,count=3,value=1,
                   replace_values=[0],expected_sha256=snapshot['sha256'])
    assert bytes(data[:12]) == bytes([1,90,90,90,1,91,91,91,2,92,92,92])
    assert patched['changed_indices'] == [0] and patched['frame_number'] == 77
    assert patched['persisted_to_save'] is False and len(writes)==1


def test_stale_hash_refuses_before_any_write(client):
    call,data,writes,_ = client
    snapshot = call('memory_table_read',address=BASE,stride=88,count=194)
    data[9000] = 2  # Changed gap, not just a field, must also invalidate the snapshot.
    with pytest.raises(ToolError,match='EXPECTED_BYTES_MISMATCH'):
        call('memory_table_patch',address=BASE,stride=88,count=194,value=1,
             replace_values=[0],expected_sha256=snapshot['sha256'])
    assert not writes


def test_multichunk_table_and_partial_write_failure(client):
    call,data,writes,emu = client
    snapshot = call('memory_table_read',address=BASE,stride=88,count=194)
    original_poke = emu.lib.poke_block
    def fail_second(*args,**kwargs):
        if writes:
            raise RuntimeError('simulated native rejection')
        return original_poke(*args,**kwargs)
    emu.lib.poke_block = fail_second
    with pytest.raises(ToolError,match='TABLE_WRITE_INCOMPLETE: 1 chunks verified'):
        call('memory_table_patch',address=BASE,stride=88,count=194,value=1,
             replace_values=[0],expected_sha256=snapshot['sha256'])
    assert data[0] == 1 and data[88*100] == 0


def test_multichunk_success_and_noop(client):
    call,data,writes,_ = client
    snap = call('memory_table_read',address=BASE,stride=88,count=194)
    result = call('memory_table_patch',address=BASE,stride=88,count=194,value=1,
                  replace_values=[0],expected_sha256=snap['sha256'])
    assert result['changed_count'] == 194 and len(writes)==5
    assert all(data[i*88]==1 and data[i*88+1]==0 for i in range(194))
    again = call('memory_table_patch',address=BASE,stride=88,count=194,value=1,
                 replace_values=[0],expected_sha256=result['sha256'])
    assert again['changed_count']==0 and len(writes)==5


@pytest.mark.parametrize('arguments',[
    {'address':0x04000000}, {'address':0x023FFFFF}, {'width':True}, {'width':3},
    {'stride':0}, {'stride':65536}, {'count':True}, {'count':4097}, {'cpu':True},
    {'count':'2'}, {'stride':1,'width':4}, {'surprise':1},
])
def test_table_invalid_inputs(client,arguments):
    call,_,writes,_ = client
    args = dict(address=BASE,stride=4,count=3)
    args.update(arguments)
    with pytest.raises(ToolError):
        call('memory_table_read',**args)
    assert not writes


@pytest.mark.parametrize('arguments',[
    {'value':256}, {'replace_values':[256]}, {'replace_values':[]},
    {'replace_values':[True]}, {'expected_sha256':'z'*64}, {'value':True},
])
def test_patch_rejects_invalid_values(client,arguments):
    call,_,writes,_=client
    args = dict(address=BASE,stride=4,count=3,value=1,replace_values=[0],expected_sha256='0'*64)
    args.update(arguments)
    with pytest.raises(ToolError): call('memory_table_patch',**args)
    assert not writes


def test_scan_chunk_boundary_alignment_and_cap(client):
    call,data,_,_ = client
    data[4095:4099] = bytes.fromhex('11223344')
    assert call('memory_scan',address=BASE,span=8192,pattern_hex='11223344')['addresses']==[BASE+4095]
    assert not call('memory_scan',address=BASE,span=8192,pattern_hex='11223344',alignment=4)['addresses']
    result=call('memory_scan',address=BASE,span=8192,pattern_hex='00')
    assert len(result['addresses'])==256 and result['truncated']


def test_workspace_and_export_never_overwrite(client,tmp_path):
    call,_,_,_ = client
    rom,save = tmp_path/'source.nds',tmp_path/'source.sav'
    header=bytearray(512);header[12:16]=b'TEST';header[0x1e]=2
    rom.write_bytes(header); save.write_bytes(b'original')
    result=call('save_workspace_prepare',rom_path=str(rom),save_path=str(save),workspace_dir=str(tmp_path/'isolated'))
    assert result['game_code']=='TEST' and result['rom_revision']==2
    assert Path(result['backup_path']).read_bytes()==b'original'
    assert Path(result['save_path']).read_bytes()==b'original'
    assert result['save_sha256']==hashlib.sha256(b'original').hexdigest()
    with pytest.raises(ToolError):
        call('save_workspace_prepare',rom_path=str(rom),save_path=str(save),workspace_dir=str(tmp_path/'isolated'))
    output=tmp_path/'new.sav'
    exported=call('backup_export',path=str(output))
    assert exported['bytes']==512 and not exported['cold_boot_verified']
    with pytest.raises(ToolError):call('backup_export',path=str(output))
    with pytest.raises(ToolError):call('backup_export',path=str(rom))
    assert save.read_bytes()==b'original' and rom.read_bytes()==header


def test_failed_export_leaves_no_destination(client,tmp_path):
    call,_,_,emu=client
    emu.lib.lib.melonds_backup_export=lambda _: 0
    with pytest.raises(ToolError,match='export failed'):
        call('backup_export',path=str(tmp_path/'failed.sav'))
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('width,value,prefix',[(1,2,'2'),(2,65535,'1'),(4,99999999,'0')])
def test_ar_direct_writes(client,width,value,prefix):
    call,_,writes,_=client
    result=call('cheat_generate_ar',address=BASE,value=value,width=width)
    assert result['code']==f'94000130 FFFB0000\n{prefix}2000000 {value:08X}\nD2000000 00000000'
    assert result['enabled'] is False and result['runtime_verified'] is False and not writes


def test_ar_loop_has_count_minus_one_and_correct_stride(client):
    call,_,_,_=client
    result=call('cheat_generate_ar',address=0x021BFB10,value=1,width=1,count=194,stride=88,activation='always')
    assert result['code'].splitlines()==['C0000000 000000C1','221BFB10 00000001','DC000000 00000058','D2000000 00000000']


@pytest.mark.parametrize('args',[{'width':1,'value':256},{'address':BASE+1,'width':2},
                                  {'count':3,'stride':3,'width':2},{'activation':'once'}, {'width':True}])
def test_ar_invalid_width_or_activation(client,args):
    call,_,writes,_=client
    fields=dict(address=BASE,value=1);fields.update(args)
    with pytest.raises(ToolError):call('cheat_generate_ar',**fields)
    assert not writes
