#!/usr/bin/env python3
import asyncio
import sys
from aiostreammagic import StreamMagicClient

AXN10_IP = "<STREAMER_IP_ADDRESS>"  # Replace with your exact AXN10 IP

async def get_now_playing():
    try:
        # Establish a quick async connection context with the streamer
        async with StreamMagicClient(AXN10_IP) as client:
            ps_obj = client.play_state
            
            if ps_obj and getattr(ps_obj, 'state', 'stopped').lower() == 'play':
                metadata = getattr(ps_obj, 'metadata', None)
                
                if metadata:
                    # Extract Track
                    track = getattr(metadata, 'title', 'Unknown Track')
                    
                    # Extract Artist using the fallback structure from your logger
                    artist = getattr(metadata, 'track_artist', None)
                    if not artist:
                        artist = getattr(metadata, 'artist', None)
                    if not artist and hasattr(metadata, '__dict__'):
                        artist = metadata.__dict__.get('performer') or metadata.__dict__.get('track_artist')
                    artist = artist or "Unknown Artist"
                    
                    # Extract Album Name
                    album = getattr(metadata, 'album', 'Unknown Album')
                    
                    print(f"Artist: {artist}")
                    print(f"Album:  {album}")
                    print(f"Track:  {track}")
                    return

            print("Status: Nothing is currently playing.")
            
    except Exception as e:
        print(f"Error connecting to AXN10: {e}", file=sys.stderr)

if __name__ == "__main__":
    asyncio.run(get_now_playing())

