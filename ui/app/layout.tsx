import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'EverCurrent — Daily Digest',
  description: 'Slack-like digest demo for hardware engineering teams',
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en">
      <body className="h-screen overflow-hidden bg-slack-purple font-sans antialiased">
        {children}
      </body>
    </html>
  )
}
