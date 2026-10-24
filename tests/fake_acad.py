"""Minimal in-memory stand-in for the AutoCAD ActiveX object model used by live.py."""

import copy
import itertools

_handles = itertools.count(0x100)


class Ent:
    def __init__(self, kind, layer="0", **kw):
        self.Handle = format(next(_handles), "X")
        self.ObjectName = kind
        self.Layer = layer
        self.deleted = False
        self.__dict__.update(kw)

    def Move(self, p1, p2):
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        for attr in ("StartPoint", "EndPoint", "InsertionPoint"):
            if hasattr(self, attr):
                p = getattr(self, attr)
                setattr(self, attr, (p[0] + dx, p[1] + dy, p[2]))
        if hasattr(self, "coords"):
            self.coords = [v + (dx if i % 2 == 0 else dy) for i, v in enumerate(self.coords)]

    def Delete(self):
        self.deleted = True


class Att:
    def __init__(self, tag, text):
        self.TagString, self.TextString = tag, text


class Collection(list):
    def __iter__(self):
        return iter([x for x in list.__iter__(self) if not getattr(x, "deleted", False)])


class Group:
    def __init__(self, name):
        self.Name, self.items, self.deleted = name, [], False

    def AppendItems(self, ents):
        self.items += list(ents)

    def __iter__(self):
        return iter([i for i in self.items if not i.deleted])

    def Delete(self):
        self.deleted = True


class Block:
    def __init__(self, name, count=0):
        self.Name, self.Count = name, count


class Layer:
    def __init__(self, name):
        self.Name, self.color = name, 7


class Doc:
    def __init__(self, name="Drawing1.dwg"):
        self.Name, self.FullName, self.Saved = name, "C:/x/" + name, True
        self.ModelSpace = self._ms()
        self.Groups = Collection()
        self.Layers = Collection([Layer("0")])
        self.Blocks = Collection()
        self.commands, self._marks, self._open = [], [], False
        self.insunits = 6
        self.vars = {}
        self.lisp_handler = lambda cmd: "nil"

    def _ms(self):
        ms = Collection()
        ms.AddLine = lambda a, b: self._add(Ent("AcDbLine", StartPoint=tuple(a), EndPoint=tuple(b)))
        ms.AddText = lambda t, p, h: self._add(
            Ent("AcDbText", TextString=t, InsertionPoint=tuple(p), Height=h))
        ms.AddLightWeightPolyline = lambda v: self._add(Ent("AcDbPolyline", coords=list(v)))
        return ms

    def _add(self, e):
        self.ModelSpace.append(e)
        return e

    def _groups_add(self, name):
        if any(g.Name.upper() == name.upper() and not g.deleted for g in self.Groups):
            raise RuntimeError("duplicate group")
        g = Group(name)
        self.Groups.append(g)
        return g

    def _layers_add(self, name):
        lyr = Layer(name)
        self.Layers.append(lyr)
        return lyr

    def StartUndoMark(self):
        self._marks.append([(e, copy.copy(e.__dict__)) for e in list.__iter__(self.ModelSpace)])
        self._gsnap = [(g, list(g.items), g.deleted) for g in list.__iter__(self.Groups)]

    def EndUndoMark(self):
        pass

    def SendCommand(self, cmd):
        self.commands.append(cmd)
        if "*acm-r*" in cmd:
            return self._lisp(cmd)
        if "UNDO" in cmd and self._marks:
            snap = self._marks.pop()
            keep = {id(e) for e, _ in snap}
            for e in list(list.__iter__(self.ModelSpace)):
                if id(e) not in keep:
                    e.deleted = True
            for e, state in snap:
                e.__dict__.update(state)
            for g, items, deleted in self._gsnap:
                g.items, g.deleted = items, deleted

    def GetVariable(self, name):
        if name == "INSUNITS":
            return self.insunits
        return self.vars.get(name, 0)

    def SetVariable(self, name, value):
        self.vars[name] = value

    def add_block(self, name, pos, attrs, layer="SYMS"):
        e = Ent("AcDbBlockReference", layer, InsertionPoint=(pos[0], pos[1], 0.0),
                EffectiveName=name, HasAttributes=bool(attrs))
        e.GetAttributes = lambda: [Att(k, v) for k, v in attrs.items()]
        return self._add(e)

    def _lisp(self, cmd):
        import re
        if "(setq *acm-r*" in cmd:
            try:
                self._acm = str(self.lisp_handler(cmd))
            except Exception as exc:  # noqa: BLE001
                self._acm = f"LISP ERROR: {exc}"
            self.vars["USERS1"] = "OK:" + self._acm[:440]
        else:
            m = re.search(r"\(substr \*acm-r\* (\d+) (\d+)\)", cmd)
            n = int(m.group(1))
            self.vars["USERS1"] = "OK:" + self._acm[n - 1:n - 1 + int(m.group(2))]

    def Save(self):
        self.Saved = True

    def SaveAs(self, path):
        self.FullName = path


class Documents:
    def __init__(self, app):
        self.app = app

    @property
    def Count(self):
        return len(self.app.docs)


class App:
    Version = "24.0"
    Caption = "AutoCAD Electrical 2026 - [Drawing1.dwg]"

    def __init__(self, doc=None):
        self.docs = [doc or Doc()]
        self.ActiveDocument = self.docs[0]
        self.Documents = Documents(self)
        self.zoomed = None
        d = self.ActiveDocument
        d.Groups.Add = d._groups_add
        d.Layers.Add = d._layers_add

    def ZoomWindow(self, a, b):
        self.zoomed = (a, b)
