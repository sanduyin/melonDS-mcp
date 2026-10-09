/*
 * platform_stubs.cpp — 无头运行时的 Platform.h 实现
 *
 * 提供真实的文件 I/O、线程与日志；多媒体（摄像头/麦克风/网络）为空实现。
 * （自 MelonMCP 移植，适配 melonDS-mcp fork）
 *
 * Copyright (C) 2026 melonDS-mcp contributors
 * Licensed under GPLv3 (same as melonDS)
 * Source: https://github.com/sanduyin/melonDS-mcp
 */

#include <cstdio>
#include <cstdarg>
#include <cstring>
#include <string>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <chrono>
#include <filesystem>
#include <fstream>

#ifdef _WIN32
#include <windows.h>
#else
#include <dlfcn.h>
#endif

#include "Platform.h"
#include "SPI_Firmware.h"

// 来自 mcp_shim.cpp
extern std::string g_save_path;

// ── 计时基准 ──
static auto g_start_time = std::chrono::steady_clock::now();

namespace melonDS::Platform
{

static FILE* OpenNativeFile(const std::string& path, const char* mode)
{
#ifdef _WIN32
    const auto nativePath = std::filesystem::u8path(path).native();
    const std::wstring nativeMode(mode, mode + std::strlen(mode));
    return _wfopen(nativePath.c_str(), nativeMode.c_str());
#else
    return fopen(path.c_str(), mode);
#endif
}

// ═══════════════════════════════════════════
// 停止信号
// ═══════════════════════════════════════════

void SignalStop(StopReason reason, void* userdata)
{
    // shim 通过 g_running 管理运行状态；此函数从模拟器核心内调用
    (void)reason;
    (void)userdata;
}

// ═══════════════════════════════════════════
// 文件 I/O — 以 FILE* 充当 FileHandle*
// ═══════════════════════════════════════════

static std::string GetModeString(FileMode mode, bool file_exists)
{
    std::string m;

    if (mode & FileMode::Append)
        m += 'a';
    else if (!(mode & FileMode::Write))
        m += 'r';
    else if (mode & FileMode::NoCreate)
        m += 'r';
    else if ((mode & FileMode::Preserve) && file_exists)
        m += 'r';
    else
        m += 'w';

    if ((mode & FileMode::ReadWrite) == FileMode::ReadWrite ||
        ((mode & FileMode::Write) && m == "r"))
        m += '+';

    if (!(mode & FileMode::Text))
        m += 'b';

    return m;
}

std::string GetLocalFilePath(const std::string& filename)
{
    return filename;
}

FileHandle* OpenFile(const std::string& path, FileMode mode)
{
    if ((mode & (FileMode::ReadWrite | FileMode::Append)) == FileMode::None)
        return nullptr;

    std::error_code error;
    bool exists = std::filesystem::exists(std::filesystem::u8path(path), error);
    if (error || ((mode & FileMode::NoCreate) && !exists)) return nullptr;
    std::string mstr = GetModeString(mode, exists);

    FILE* f = OpenNativeFile(path, mstr.c_str());
    return reinterpret_cast<FileHandle*>(f);
}

FileHandle* OpenLocalFile(const std::string& path, FileMode mode)
{
    return OpenFile(path, mode);
}

bool FileExists(const std::string& name)
{
    std::error_code error;
    return std::filesystem::exists(std::filesystem::u8path(name), error) && !error;
}

bool LocalFileExists(const std::string& name)
{
    return FileExists(name);
}

bool CheckFileWritable(const std::string& filepath)
{
    FILE* f = OpenNativeFile(filepath, "ab");
    if (f) { fclose(f); return true; }
    return false;
}

bool CheckLocalFileWritable(const std::string& filepath)
{
    return CheckFileWritable(filepath);
}

bool CloseFile(FileHandle* file)
{
    return fclose(reinterpret_cast<FILE*>(file)) == 0;
}

bool IsEndOfFile(FileHandle* file)
{
    return feof(reinterpret_cast<FILE*>(file)) != 0;
}

bool FileReadLine(char* str, int count, FileHandle* file)
{
    return fgets(str, count, reinterpret_cast<FILE*>(file)) != nullptr;
}

u64 FilePosition(FileHandle* file)
{
#ifdef _WIN32
    return static_cast<u64>(_ftelli64(reinterpret_cast<FILE*>(file)));
#else
    return static_cast<u64>(ftello(reinterpret_cast<FILE*>(file)));
#endif
}

bool FileSeek(FileHandle* file, s64 offset, FileSeekOrigin origin)
{
    int whence;
    switch (origin) {
        case FileSeekOrigin::Start:   whence = SEEK_SET; break;
        case FileSeekOrigin::Current: whence = SEEK_CUR; break;
        case FileSeekOrigin::End:     whence = SEEK_END; break;
        default:                      whence = SEEK_SET; break;
    }
#ifdef _WIN32
    return _fseeki64(reinterpret_cast<FILE*>(file), offset, whence) == 0;
#else
    return fseeko(reinterpret_cast<FILE*>(file), static_cast<off_t>(offset), whence) == 0;
#endif
}

void FileRewind(FileHandle* file)
{
    rewind(reinterpret_cast<FILE*>(file));
}

u64 FileRead(void* data, u64 size, u64 count, FileHandle* file)
{
    return fread(data, size, count, reinterpret_cast<FILE*>(file));
}

bool FileFlush(FileHandle* file)
{
    return fflush(reinterpret_cast<FILE*>(file)) == 0;
}

u64 FileWrite(const void* data, u64 size, u64 count, FileHandle* file)
{
    return fwrite(data, size, count, reinterpret_cast<FILE*>(file));
}

u64 FileWriteFormatted(FileHandle* file, const char* fmt, ...)
{
    if (!fmt) return 0;
    va_list args;
    va_start(args, fmt);
    u64 ret = vfprintf(reinterpret_cast<FILE*>(file), fmt, args);
    va_end(args);
    return ret;
}

u64 FileLength(FileHandle* file)
{
    const u64 pos = FilePosition(file);
    if (!FileSeek(file, 0, FileSeekOrigin::End)) return 0;
    const u64 len = FilePosition(file);
    if (!FileSeek(file, static_cast<s64>(pos), FileSeekOrigin::Start)) return 0;
    return len;
}

// ═══════════════════════════════════════════
// 日志
// ═══════════════════════════════════════════

void Log(LogLevel level, const char* fmt, ...)
{
    if (!fmt) return;
    va_list args;
    va_start(args, fmt);
    vfprintf(stderr, fmt, args);
    va_end(args);
}

// ═══════════════════════════════════════════
// 线程
// ═══════════════════════════════════════════

struct ThreadImpl
{
    std::thread t;
    ThreadImpl(std::function<void()> func) : t(std::move(func)) {}
};

Thread* Thread_Create(std::function<void()> func)
{
    auto* impl = new ThreadImpl(std::move(func));
    return reinterpret_cast<Thread*>(impl);
}

void Thread_Free(Thread* thread)
{
    auto* impl = reinterpret_cast<ThreadImpl*>(thread);
    if (impl->t.joinable())
        impl->t.detach();
    delete impl;
}

void Thread_Wait(Thread* thread)
{
    auto* impl = reinterpret_cast<ThreadImpl*>(thread);
    if (impl->t.joinable())
        impl->t.join();
}

struct SemaphoreImpl
{
    std::mutex mtx;
    std::condition_variable cv;
    int count = 0;
};

Semaphore* Semaphore_Create()
{
    return reinterpret_cast<Semaphore*>(new SemaphoreImpl());
}

void Semaphore_Free(Semaphore* sema)
{
    delete reinterpret_cast<SemaphoreImpl*>(sema);
}

void Semaphore_Reset(Semaphore* sema)
{
    auto* s = reinterpret_cast<SemaphoreImpl*>(sema);
    std::lock_guard<std::mutex> lock(s->mtx);
    s->count = 0;
}

void Semaphore_Wait(Semaphore* sema)
{
    auto* s = reinterpret_cast<SemaphoreImpl*>(sema);
    std::unique_lock<std::mutex> lock(s->mtx);
    s->cv.wait(lock, [s] { return s->count > 0; });
    s->count--;
}

bool Semaphore_TryWait(Semaphore* sema, int timeout_ms)
{
    auto* s = reinterpret_cast<SemaphoreImpl*>(sema);
    std::unique_lock<std::mutex> lock(s->mtx);

    if (timeout_ms == 0) {
        if (s->count > 0) { s->count--; return true; }
        return false;
    }

    bool got = s->cv.wait_for(lock, std::chrono::milliseconds(timeout_ms),
                               [s] { return s->count > 0; });
    if (got) s->count--;
    return got;
}

void Semaphore_Post(Semaphore* sema, int count)
{
    auto* s = reinterpret_cast<SemaphoreImpl*>(sema);
    {
        std::lock_guard<std::mutex> lock(s->mtx);
        s->count += count;
    }
    for (int i = 0; i < count; i++)
        s->cv.notify_one();
}

Mutex* Mutex_Create()
{
    return reinterpret_cast<Mutex*>(new std::mutex());
}

void Mutex_Free(Mutex* mutex)
{
    delete reinterpret_cast<std::mutex*>(mutex);
}

void Mutex_Lock(Mutex* mutex)
{
    reinterpret_cast<std::mutex*>(mutex)->lock();
}

void Mutex_Unlock(Mutex* mutex)
{
    reinterpret_cast<std::mutex*>(mutex)->unlock();
}

bool Mutex_TryLock(Mutex* mutex)
{
    return reinterpret_cast<std::mutex*>(mutex)->try_lock();
}

// ═══════════════════════════════════════════
// 计时
// ═══════════════════════════════════════════

void Sleep(u64 usecs)
{
    std::this_thread::sleep_for(std::chrono::microseconds(usecs));
}

u64 GetMSCount()
{
    auto now = std::chrono::steady_clock::now();
    return std::chrono::duration_cast<std::chrono::milliseconds>(now - g_start_time).count();
}

u64 GetUSCount()
{
    auto now = std::chrono::steady_clock::now();
    return std::chrono::duration_cast<std::chrono::microseconds>(now - g_start_time).count();
}

// ═══════════════════════════════════════════
// 存档回调
// ═══════════════════════════════════════════

void WriteNDSSave(const u8* savedata, u32 savelen, u32 writeoffset, u32 writelen, void* userdata)
{
    (void)writeoffset;
    (void)writelen;
    (void)userdata;

    if (g_save_path.empty() || !savedata || savelen == 0) return;

    FILE* f = OpenNativeFile(g_save_path, "wb");
    if (f) {
        fwrite(savedata, 1, savelen, f);
        fclose(f);
    }
}

void WriteGBASave(const u8* savedata, u32 savelen, u32 writeoffset, u32 writelen, void* userdata)
{
    (void)savedata; (void)savelen; (void)writeoffset; (void)writelen; (void)userdata;
}

void WriteFirmware(const Firmware& firmware, u32 writeoffset, u32 writelen, void* userdata)
{
    (void)firmware; (void)writeoffset; (void)writelen; (void)userdata;
}

void WriteDateTime(int year, int month, int day, int hour, int minute, int second, void* userdata)
{
    (void)year; (void)month; (void)day; (void)hour; (void)minute; (void)second; (void)userdata;
}

// ═══════════════════════════════════════════
// 多人游戏 — 空实现
// ═══════════════════════════════════════════

void MP_Begin(void* userdata) { (void)userdata; }
void MP_End(void* userdata) { (void)userdata; }
int MP_SendPacket(u8* data, int len, u64 timestamp, void* userdata)
{ (void)data; (void)len; (void)timestamp; (void)userdata; return 0; }
int MP_RecvPacket(u8* data, u64* timestamp, void* userdata)
{ (void)data; (void)timestamp; (void)userdata; return 0; }
int MP_SendCmd(u8* data, int len, u64 timestamp, void* userdata)
{ (void)data; (void)len; (void)timestamp; (void)userdata; return 0; }
int MP_SendReply(u8* data, int len, u64 timestamp, u16 aid, void* userdata)
{ (void)data; (void)len; (void)timestamp; (void)aid; (void)userdata; return 0; }
int MP_SendAck(u8* data, int len, u64 timestamp, void* userdata)
{ (void)data; (void)len; (void)timestamp; (void)userdata; return 0; }
int MP_RecvHostPacket(u8* data, u64* timestamp, void* userdata)
{ (void)data; (void)timestamp; (void)userdata; return 0; }
u16 MP_RecvReplies(u8* data, u64 timestamp, u16 aidmask, void* userdata)
{ (void)data; (void)timestamp; (void)aidmask; (void)userdata; return 0; }

// ═══════════════════════════════════════════
// 网络 — 空实现
// ═══════════════════════════════════════════

int Net_SendPacket(u8* data, int len, void* userdata)
{ (void)data; (void)len; (void)userdata; return 0; }
int Net_RecvPacket(u8* data, void* userdata)
{ (void)data; (void)userdata; return 0; }

// ═══════════════════════════════════════════
// 摄像头 — 空实现
// ═══════════════════════════════════════════

void Camera_Start(int num, void* userdata) { (void)num; (void)userdata; }
void Camera_Stop(int num, void* userdata) { (void)num; (void)userdata; }
void Camera_CaptureFrame(int num, u32* frame, int width, int height, bool yuv, void* userdata)
{ (void)num; (void)frame; (void)width; (void)height; (void)yuv; (void)userdata; }

// ═══════════════════════════════════════════
// 麦克风 — 空实现
// ═══════════════════════════════════════════

void Mic_Start(void* userdata) { (void)userdata; }
void Mic_Stop(void* userdata) { (void)userdata; }
int Mic_ReadInput(s16* data, int maxlength, void* userdata)
{ (void)data; (void)maxlength; (void)userdata; return 0; }

// ═══════════════════════════════════════════
// AAC — 空实现（仅 DSi）
// ═══════════════════════════════════════════

AACDecoder* AAC_Init() { return nullptr; }
void AAC_DeInit(AACDecoder* dec) { (void)dec; }
bool AAC_Configure(AACDecoder* dec, int frequency, int channels)
{ (void)dec; (void)frequency; (void)channels; return false; }
bool AAC_DecodeFrame(AACDecoder* dec, const void* input, int inputlen, void* output, int outputlen)
{ (void)dec; (void)input; (void)inputlen; (void)output; (void)outputlen; return false; }

// ═══════════════════════════════════════════
// 外设输入 — 空实现
// ═══════════════════════════════════════════

bool Addon_KeyDown(KeyType type, void* userdata)
{ (void)type; (void)userdata; return false; }
void Addon_RumbleStart(u32 len, void* userdata)
{ (void)len; (void)userdata; }
void Addon_RumbleStop(void* userdata)
{ (void)userdata; }
float Addon_MotionQuery(MotionQueryType type, void* userdata)
{ (void)type; (void)userdata; return 0.0f; }

// ═══════════════════════════════════════════
// 动态库加载
// ═══════════════════════════════════════════

DynamicLibrary* DynamicLibrary_Load(const char* lib)
{
#ifdef _WIN32
    if (!lib) return nullptr;
    const auto nativePath = std::filesystem::u8path(lib).native();
    const HMODULE handle = LoadLibraryW(nativePath.c_str());
#else
    void* handle = dlopen(lib, RTLD_LAZY);
#endif
    return reinterpret_cast<DynamicLibrary*>(handle);
}

void DynamicLibrary_Unload(DynamicLibrary* lib)
{
    if (!lib) return;
#ifdef _WIN32
    FreeLibrary(reinterpret_cast<HMODULE>(lib));
#else
    dlclose(reinterpret_cast<void*>(lib));
#endif
}

void* DynamicLibrary_LoadFunction(DynamicLibrary* lib, const char* name)
{
    if (!lib) return nullptr;
#ifdef _WIN32
    return reinterpret_cast<void*>(GetProcAddress(reinterpret_cast<HMODULE>(lib), name));
#else
    return dlsym(reinterpret_cast<void*>(lib), name);
#endif
}

} // namespace melonDS::Platform
