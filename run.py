"""Run the local demo, with optional explicitly selected real MAX settings."""
import argparse
import uvicorn
from app.main import create_app
from app.max_settings import MaxSettings

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--max-config')
    args = parser.parse_args()
    settings = MaxSettings.load(args.max_config) if args.max_config else None
    uvicorn.run(create_app(max_settings=settings), host='127.0.0.1', port=5211, access_log=False)
