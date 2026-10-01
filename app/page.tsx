export default function Home() {
  const engineUrl = (process.env.NEXT_PUBLIC_FPL_ENGINE_URL || "https://pitchiq-python-production.up.railway.app").replace(/\/$/, "");

  return (
    <main style={{ minHeight: "100vh", padding: 0, margin: 0 }}>
      <iframe
        title="FPL PitchIQ"
        src={engineUrl}
        style={{ display: "block", width: "100%", height: "100vh", border: 0 }}
      />
    </main>
  );
}
