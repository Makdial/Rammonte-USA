"""Seed a self-contained demo dataset for recording the walkthrough videos.

Run with the project's own interpreter, from ``rammonte_usa``::

    python ../tools/demo/seed_demo.py

It creates users (member / read-only staff / admin) and a handful of videos
whose media files are generated locally, so nothing depends on Cloudflare R2
or the network. Everything it writes is tagged so it can be removed again:

    python ../tools/demo/seed_demo.py --clean
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import django

# Make the project importable no matter where the script is launched from.
PROJECT_ROOT = Path(__file__).resolve().parents[2] / "rammonte_usa"
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rammonte_usa.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.core.files.base import ContentFile  # noqa: E402

from accounts.models import Profile  # noqa: E402
from videos.models import (  # noqa: E402
    Comment,
    Playlist,
    PlaylistItem,
    SubtitleTrack,
    Video,
)

User = get_user_model()

MEDIA_ROOT = Path(settings.MEDIA_ROOT)
DEMO_MARK = "[demo]"

DEMO_USERS = [
    # username, password, access_level, is_superuser, display name
    ("demo_viewer", "demo-viewer-123", Profile.Access.MEMBER, False),
    ("demo_staff", "demo-staff-123", Profile.Access.READ, True),
    ("demo_admin", "demo-admin-123", Profile.Access.WRITE, True),
]

VIDEOS = [
    {
        "title": "Xassida: Nahnu fi Dhilli Rasulillah",
        "description": (
            "A recorded xassida session. Turn on captions with the CC button "
            "under the player to read along in Wolof, French or Arabic."
        ),
        "duration": 8,
        "tracks": [
            ("wo", "Wolof", False, "Xassida bi da faÃ±u wax ci lu am solo ci biir xol."),
            ("fr", "French", False, "Cette xassida raconte ce qui compte vraiment."),
            ("ar", "Arabic", False, "Ù‡Ø°Ù‡ Ø§Ù„Ù‚ØµÙŠØ¯Ø© ØªØ­ÙƒÙŠ Ø¹Ù…Ø§ ÙŠÙ‡Ù… Ø­Ù‚Ø§."),
            ("lyrics", "Lyrics", True, "â™ª Nahnu fi dhille rasulillah â™ª"),
        ],
    },
    {
        "title": "Cours du soir: les fondements",
        "description": (
            "An evening class on the basics. Listen to it as audio, save it "
            "offline, and download the MP3 for later."
        ),
        "duration": 6,
        "tracks": [
            ("fr", "French", False, "Le cours du soir porte sur les fondements."),
            ("en", "English", False, "The evening class covers the foundations."),
        ],
    },
    {
        "title": "Jumaa khutba, Diourbel",
        "description": (
            "Friday sermon recorded in Diourbel. Includes an auto-generated "
            "transcript you can switch on while watching."
        ),
        "duration": 7,
        "tracks": [
            ("ar", "Arabic", False, "Ø®Ø·Ø¨Ø© Ø§Ù„Ø¬Ù…Ø¹Ø© Ù…Ù† Ø¯ÙŠÙˆØ±Ø¨Ù„."),
            ("fr", "French", False, "La khutba du vendredi, enregistrÃ©e Ã  Diourbel."),
        ],
    },
    {
        "title": "Zikr after Maghrib",
        "description": "A short zikr recitation. Good for testing playback speed.",
        "duration": 5,
        "tracks": [
            ("wo", "Wolof", False, "Zikr gi ci ginnaaw Maghrib."),
        ],
    },
    {
        "title": "Interview: l'importance de la transmission",
        "description": (
            "A conversation about passing knowledge on. Comment below, and "
            "save it to a playlist."
        ),
        "duration": 9,
        "tracks": [
            ("fr", "French", False, "L'importance de transmettre le savoir."),
            ("en", "English", False, "The importance of passing knowledge on."),
        ],
    },
]

COMMENTS = [
    "Barakallahu feek, very useful.",
    "Merci, je cherchais exactement Ã§a.",
    "Please upload the rest of the series.",
    "JÃ«rÃ«jÃ«f ci sa liggÃ©ey.",
]


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def make_video_file(path: Path, seconds: int, colour: str) -> None:
    """Render a small 640x360 test video with a visible timer."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_exe(), "-y", "-v", "error",
            "-f", "lavfi", "-i",
            f"color=c={colour}:s=640x360:d={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-vf", "drawtext=text='Rammonte USA':fontsize=48:fontcolor=white:"
                   "x=(w-tw)/2:y=(h-th)/2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )


def make_audio_file(path: Path, seconds: int) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_exe(), "-y", "-v", "error",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:a", "libmp3lame", "-q:a", "6", str(path),
        ],
        check=True,
    )


def webvtt(lines: list[str], total: int) -> str:
    """Spread the lines across the media duration."""
    parts = ["WEBVTT", ""]
    count = max(len(lines), 1)
    step = total / count
    for index, text in enumerate(lines):
        start = index * step
        end = min(start + step - 0.2, total)
        parts.append(f"{stamp(start)} --> {stamp(end)}")
        parts.append(text)
        parts.append("")
    return "\n".join(parts)


def stamp(seconds: float) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours:02d}:{int(minutes):02d}:{secs:06.3f}"


def seed() -> None:
    print("== creating demo users ==")
    users = {}
    for username, password, access, is_super in DEMO_USERS:
        user, _ = User.objects.get_or_create(username=username)
        user.set_password(password)
        user.is_superuser = is_super
        user.is_staff = access != Profile.Access.MEMBER
        user.save()
        Profile.objects.update_or_create(
            user=user, defaults={"access_level": access}
        )
        users[username] = user
        print(f"   {username:14s} pw={password:16s} access={access}")

    admin = users["demo_admin"]
    viewer = users["demo_viewer"]

    print("== creating demo videos ==")
    made = []
    for index, spec in enumerate(VIDEOS):
        title = f"{DEMO_MARK} {spec['title']}"
        video, _ = Video.objects.get_or_create(
            title=title, user=admin,
            defaults={
                "description": spec["description"],
                "media_type": "video",
                "duration_seconds": spec["duration"],
                "audio_status": "ready",
                "auto_subtitle_status": "ready",
                "position": index,
            },
        )
        video.description = spec["description"]
        video.position = index
        video.duration_seconds = spec["duration"]
        video.save()

        colour = ["0x1a3a5c", "0x5c1a3a", "0x1a5c3a", "0x5c4a1a", "0x3a1a5c"][
            index % 5
        ]
        vpath = MEDIA_ROOT / "demo" / f"video_{video.id}.mp4"
        apath = MEDIA_ROOT / "demo" / f"audio_{video.id}.mp3"
        tpath = MEDIA_ROOT / "demo" / f"thumb_{video.id}.jpg"
        make_video_file(vpath, spec["duration"], colour)
        make_audio_file(apath, spec["duration"])
        if not tpath.exists():
            subprocess.run(
                [
                    ffmpeg_exe(), "-y", "-v", "error", "-i", str(vpath),
                    "-frames:v", "1", "-vf", "scale=320:180", str(tpath),
                ],
                check=True,
            )

        video.file_id = f"demo/video_{video.id}.mp4"
        video.video_url = settings.MEDIA_URL + video.file_id
        video.thumbnail_file_id = f"demo/thumb_{video.id}.jpg"
        video.thumbnail_url = settings.MEDIA_URL + video.thumbnail_file_id
        video.audio_file_id = f"demo/audio_{video.id}.mp3"
        video.audio_url = settings.MEDIA_URL + video.audio_file_id
        video.save()

        for code, label, is_lyrics, text in spec["tracks"]:
            SubtitleTrack.objects.update_or_create(
                video=video, language_code=code,
                defaults={
                    "label": label,
                    "is_lyrics": is_lyrics,
                    "content": webvtt([text] * 3, spec["duration"]),
                },
            )

        for body in COMMENTS[:2 + (index % 2)]:
            Comment.objects.get_or_create(video=video, user=viewer, body=body)

        made.append(video)
        print(f"   #{video.id} {spec['title']}")

    print("== creating a demo playlist ==")
    playlist, _ = Playlist.objects.get_or_create(
        user=viewer,
        title=f"{DEMO_MARK} Mes favoris",
        defaults={"description": "A demo playlist.", "is_public": True},
    )
    PlaylistItem.objects.filter(playlist=playlist).delete()
    for position, video in enumerate(made[:3]):
        PlaylistItem.objects.create(
            playlist=playlist, video=video, position=position
        )

    print("\n== ready ==")
    print("   viewer : demo_viewer / demo-viewer-123")
    print("   staff  : demo_staff  / demo-staff-123  (read-only, dashboard access)")
    print("   admin  : demo_admin  / demo-admin-123  (read & write)")
    print(f"   videos : {', '.join(str(v.id) for v in made)}")
    print("   remove : python ../tools/demo/seed_demo.py --clean")


def clean() -> None:
    print("== removing demo data ==")
    removed = Video.objects.filter(title__startswith=DEMO_MARK).delete()
    print(f"   videos: {removed}")
    Playlist.objects.filter(title__startswith=DEMO_MARK).delete()
    User.objects.filter(username__in=[u[0] for u in DEMO_USERS]).delete()
    demo_dir = MEDIA_ROOT / "demo"
    if demo_dir.exists():
        for item in demo_dir.iterdir():
            item.unlink()
        demo_dir.rmdir()
    print("   done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    if args.clean:
        clean()
    else:
        seed()
