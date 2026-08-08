import { ImageResponse } from "next/og";

export const alt = "Garmin Coach personal training dashboard";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpenGraphImage() {
  return new ImageResponse(
    <div
      style={{
        alignItems: "center",
        background: "#0a0a0a",
        color: "#fafafa",
        display: "flex",
        height: "100%",
        justifyContent: "center",
        padding: "80px",
        width: "100%",
      }}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "24px",
          maxWidth: "920px",
          textAlign: "center",
        }}
      >
        <div style={{ fontSize: 72, fontWeight: 700, letterSpacing: "-0.04em" }}>Garmin Coach</div>
        <div style={{ color: "#a3a3a3", fontSize: 32 }}>Your personal training dashboard</div>
      </div>
    </div>,
    size,
  );
}
