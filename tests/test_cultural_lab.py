from pathlib import Path
import numpy as np
import pytest
import trimesh
from PIL import Image,ImageDraw
from backend import cultural_lab as lab

@pytest.mark.parametrize('theme',lab.THEMES)
@pytest.mark.parametrize('product,x,y,z',[(lab.PRODUCTS[0],82,82,100),(lab.PRODUCTS[1],130,32,2.8),(lab.PRODUCTS[2],166,32,3),(lab.PRODUCTS[3],150,30,3)])
def test_products_are_closed_and_have_through_holes(product,x,y,z,theme,tmp_path):
    mesh=lab.design(product,theme,x,y,z,4,.85,True)
    dest=tmp_path/'part.stl';mesh.export(dest);result=trimesh.load(dest,force='mesh')
    assert result.is_watertight and result.is_volume
    assert len(result.split(only_watertight=False))==1
    if product==lab.PRODUCTS[3]:assert result.extents[0]==pytest.approx(x,abs=.02)
    if product != lab.PRODUCTS[0]:
        px=(.85-.5)*x*.6
        assert not result.contains([[px,0,z/2]])[0]

def test_custom_logo_is_real_geometry(tmp_path):
    path=tmp_path/'logo.png';im=Image.new('RGB',(100,100),'white');ImageDraw.Draw(im).rectangle((20,20,80,80),fill='black');im.save(path)
    plain=lab.design(lab.PRODUCTS[1],lab.THEMES[0],130,32,3,0,.5,False)
    custom=lab.design(lab.PRODUCTS[1],lab.THEMES[0],130,32,3,0,.5,False,str(path))
    assert custom.is_watertight and abs(custom.volume-plain.volume)>5

def test_ruler_one_mm_ticks():
    m=lab.design(lab.PRODUCTS[2],lab.THEMES[0],166,32,3,0,.5,False)
    origins=[]
    for i in range(11): origins.extend([[-75+i,-13.8,9],[-74.5+i,-13.8,9]])
    hits,ids,_=m.ray.intersects_location(origins,np.tile([0,0,-1],(len(origins),1)),multiple_hits=False)
    heights={int(i):p[2] for p,i in zip(hits,ids)}
    for i in range(11):
        assert heights[2*i]==pytest.approx(2.4,abs=.02)
        assert heights[2*i+1]==pytest.approx(3,abs=.02)

def test_cpu_preview_rotation_and_batch(tmp_path,monkeypatch):
    import backend.env as env
    monkeypatch.setattr(env,'ROOT',tmp_path)
    preview,stl,archive,status,image,state=lab.run(lab.PRODUCTS[0],lab.THEMES[0],82,82,100,0,.5,True,None,True,lab.STYLES[0],3.2,10,1.2)
    assert all(Path(p).is_file() for p in (preview,stl,archive,image))
    rotated,next_state=lab.rotate_static(state,1)
    assert next_state[1]==1 and Path(rotated).is_file()
    assert Path(rotated).read_bytes()!=Path(image).read_bytes()
