rm -rf build/
python setup.py install
rm -rf build/temp*
mv build/*/*.so ./
mv build/*/*.pyd ./
rm -rf build/