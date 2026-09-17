import { useEffect, useState, useCallback } from "react";

interface AirPlayVideo extends HTMLVideoElement {
  webkitShowPlaybackTargetPicker?: () => void;
  webkitCurrentPlaybackTargetIsWireless?: boolean;
}

interface PlaybackTargetAvailabilityEvent extends Event {
  availability: "available" | "not-available";
}

export function useAirPlay(
  videoRef: React.RefObject<HTMLVideoElement | null>,
  remoteUrl: string | null,
) {
  const [available, setAvailable] = useState(false);
  const [active, setActive] = useState(false);

  useEffect(() => {
    const video = videoRef.current as AirPlayVideo | null;
    if (!video || !remoteUrl) return;

    video.setAttribute("x-webkit-airplay", "allow");

    // hls.js plays through MSE, which Safari will not hand to an Apple TV. A
    // <source> child holding the real playlist URL gives AirPlay something it
    // can send instead, and the Apple TV then fetches the stream itself.
    const source = document.createElement("source");
    source.src = remoteUrl;
    source.type = "application/x-mpegurl";
    video.appendChild(source);

    const onAvailability = (e: Event) => {
      setAvailable((e as PlaybackTargetAvailabilityEvent).availability === "available");
    };
    const onTargetChange = () => setActive(!!video.webkitCurrentPlaybackTargetIsWireless);

    video.addEventListener("webkitplaybacktargetavailabilitychanged", onAvailability);
    video.addEventListener("webkitcurrentplaybacktargetiswirelesschanged", onTargetChange);

    return () => {
      video.removeEventListener("webkitplaybacktargetavailabilitychanged", onAvailability);
      video.removeEventListener("webkitcurrentplaybacktargetiswirelesschanged", onTargetChange);
      source.remove();
    };
  }, [videoRef, remoteUrl]);

  const showPicker = useCallback(() => {
    const video = videoRef.current as AirPlayVideo | null;
    video?.webkitShowPlaybackTargetPicker?.();
  }, [videoRef]);

  return { available, active, showPicker };
}
