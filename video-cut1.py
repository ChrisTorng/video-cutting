import argparse
import ffmpeg
import os
import re
import tempfile
import shutil
from typing import List, Tuple

def parse_time(time_str: str) -> float:
    match = re.match(r'(\d+):(\d+)(?::(\d+(?:\.\d+)?))?', time_str)
    if not match:
        raise ValueError(f"Invalid time: {time_str}")
    h, m, s = match.groups(default='0')
    return int(h) * 3600 + int(m) * 60 + float(s)

def parse_time_ranges_file(filepath: str) -> List[Tuple[float, float]]:
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f if line.strip()]
    ranges = []
    for line in lines:
        m = re.match(r'([\d:.]+)-([\d:.]+)', line)
        if not m:
            raise ValueError(f"Invalid time range: {line}")
        start, end = parse_time(m.group(1)), parse_time(m.group(2))
        if start >= end:
            raise ValueError(f"Start time >= end time: {line}")
        ranges.append((start, end))
    # 檢查排序與重疊
    for i in range(1, len(ranges)):
        if ranges[i-1][1] > ranges[i][0]:
            raise ValueError("Time ranges are not sorted or overlap")
    return ranges

def get_min_duration(ranges: List[Tuple[float, float]]) -> float:
    return min(end - start for start, end in ranges)

def cut_and_fade_segments(video_file, ranges, overlap_time, temp_dir):
    segment_files = []
    for idx, (start, end) in enumerate(ranges):
        duration = end - start
        seg_path = os.path.join(temp_dir, f'segment_{idx}.mp4')
        fade_in = fade_out = ''
        # 處理淡入淡出
        if overlap_time > 0:
            fade_in = f"fade=t=in:st=0:d={overlap_time}"
            fade_out = f"fade=t=out:st={duration-overlap_time}:d={overlap_time}"
            vf = f"{fade_in},{fade_out}"
            af = f"afade=t=in:st=0:d={overlap_time},afade=t=out:st={duration-overlap_time}:d={overlap_time}"
            (
                ffmpeg
                .input(video_file, ss=start, t=duration)
                .output(seg_path, vcodec='libx264', acodec='aac', vf=vf, af=af, strict='experimental', movflags='faststart', loglevel='error')
                .overwrite_output()
                .run()
            )
        else:
            (
                ffmpeg
                .input(video_file, ss=start, t=duration)
                .output(seg_path, vcodec='copy', acodec='copy', movflags='faststart', loglevel='error')
                .overwrite_output()
                .run()
            )
        segment_files.append(seg_path)
    return segment_files

def concat_segments(segment_files, output_file):
    # 建立 concat 檔案
    concat_file = output_file + '.concat.txt'
    with open(concat_file, 'w', encoding='utf-8') as f:
        for seg in segment_files:
            f.write(f"file '{os.path.abspath(seg)}'\n")
    (
        ffmpeg
        .input(concat_file, format='concat', safe=0)
        .output(output_file, c='copy', movflags='faststart', loglevel='error')
        .overwrite_output()
        .run()
    )
    os.remove(concat_file)

def adjust_subtitle(srt_file, ranges, output_srt):
    # 讀取原始字幕
    with open(srt_file, 'r', encoding='utf-8') as f:
        srt_lines = f.readlines()
    pattern = re.compile(r'(\d{2}):(\d{2}):(\d{2}),(\d{3})')
    def srt_time_to_sec(h, m, s, ms):
        return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000
    def sec_to_srt_time(sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int(round((sec - int(sec)) * 1000))
        return f"{h:02}:{m:02}:{s:02},{ms:03}"
    # 解析字幕區間
    new_srt = []
    idx = 1
    for seg_idx, (start, end) in enumerate(ranges):
        seg_offset = sum(r[1]-r[0] for r in ranges[:seg_idx])
        for i in range(len(srt_lines)):
            line = srt_lines[i]
            if '-->' in line:
                t1, t2 = line.split('-->')
                m1 = pattern.findall(t1)
                m2 = pattern.findall(t2)
                if m1 and m2:
                    st = srt_time_to_sec(*m1[0])
                    et = srt_time_to_sec(*m2[0])
                    # 若字幕在此片段範圍內
                    if st >= start and et <= end:
                        # 調整時間
                        new_st = sec_to_srt_time(st - start + seg_offset)
                        new_et = sec_to_srt_time(et - start + seg_offset)
                        new_srt.append(f"{idx}\n")
                        new_srt.append(f"{new_st} --> {new_et}\n")
                        # 取出字幕內容
                        j = i + 1
                        while j < len(srt_lines) and srt_lines[j].strip():
                            new_srt.append(srt_lines[j])
                            j += 1
                        new_srt.append('\n')
                        idx += 1
    with open(output_srt, 'w', encoding='utf-8') as f:
        f.writelines(new_srt)

def mux_subtitle(video_file, srt_file, output_file):
    (
        ffmpeg
        .input(video_file)
        .output(output_file, vcodec='copy', acodec='copy', movflags='faststart', loglevel='error', **{'scodec': 'mov_text', 's': srt_file})
        .overwrite_output()
        .run()
    )

def main():
    parser = argparse.ArgumentParser(description='影片切割工具')
    parser.add_argument('video_file', help='輸入影片檔')
    parser.add_argument('time_ranges_file', help='時間切割檔')
    parser.add_argument('-o', '--output', help='輸出 mp4 檔名')
    parser.add_argument('-ov', '--overlap', type=float, default=0, help='重疊漸變時間(秒)')
    parser.add_argument('-s', '--subtitle', help='字幕檔 (srt)')
    args = parser.parse_args()

    # 解析時間切割檔
    try:
        ranges = parse_time_ranges_file(args.time_ranges_file)
    except Exception as e:
        print(f"時間切割檔錯誤: {e}")
        exit(1)
    min_dur = get_min_duration(ranges)
    if args.overlap > min_dur:
        print("漸變時間不可大於最小切割區間")
        exit(1)
    # 決定輸出檔名
    if args.output:
        output_file = args.output
    else:
        base = os.path.splitext(os.path.basename(args.time_ranges_file))[0]
        output_file = base + '.mp4'
    with tempfile.TemporaryDirectory() as temp_dir:
        # 切割片段
        segment_files = cut_and_fade_segments(args.video_file, ranges, args.overlap, temp_dir)
        # 合併片段
        concat_segments(segment_files, output_file)
        # 處理字幕
        if args.subtitle:
            temp_srt = os.path.join(temp_dir, 'output.srt')
            adjust_subtitle(args.subtitle, ranges, temp_srt)
            temp_mux = os.path.join(temp_dir, 'mux.mp4')
            mux_subtitle(output_file, temp_srt, temp_mux)
            shutil.move(temp_mux, output_file)
    print(f"輸出完成: {output_file}")

if __name__ == '__main__':
    main()