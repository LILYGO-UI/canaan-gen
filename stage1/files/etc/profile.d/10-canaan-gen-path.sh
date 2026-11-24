#!/bin/sh

# Debian places administrative networking tools such as ifconfig in /usr/sbin.
# Keep both administrative directories available in an interactive login shell
# while preserving any path supplied by the user or the image environment.
case ":${PATH:-}:" in
    *:/usr/sbin:*) ;;
    *) PATH="${PATH:+$PATH:}/usr/sbin" ;;
esac
case ":${PATH:-}:" in
    *:/sbin:*) ;;
    *) PATH="${PATH:+$PATH:}/sbin" ;;
esac
export PATH
