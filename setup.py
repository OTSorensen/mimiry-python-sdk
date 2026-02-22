from setuptools import setup, find_packages

setup(
    name="mimiry",
    version="0.1.0",
    description="Python SDK for the Mimiry GPU Cloud API",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Mimiry",
    url="https://github.com/OTSorensen/mimiry-python-sdk",
    project_urls={
        "Documentation": "https://mimiryprimary.lovable.app",
        "Source": "https://github.com/OTSorensen/mimiry-python-sdk",
        "Bug Tracker": "https://github.com/OTSorensen/mimiry-python-sdk/issues",
    },
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "httpx>=0.24.0",
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
    keywords="gpu cloud api sdk mimiry machine-learning",
)
