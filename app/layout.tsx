import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata={title:"FPL PitchIQ — Your best FPL move, explained.",description:"Compare transfers, rolling, captaincy and squad decisions using live FPL data and transparent projections."};
export default function RootLayout({children}:{children:React.ReactNode}){return <html lang="en"><body>{children}</body></html>}