import logging

from pxr import Usd, Tf, Sdf
from Qt import QtCore, QtWidgets, QtGui

from .lib.qt import schedule, report_error
from .lib.usd import remove_spec, LIST_ATTRS
from .lib.usd_merge_spec import copy_spec_merge
from .tree.simpletree import TreeModel, Item
from .prim_type_icons import PrimTypeIconProvider


log = logging.getLogger(__name__)


def shorten(s, width, placeholder="..."):
    """Shorten string to `width`"""
    if len(s) <= width:
        return s
    return "{}{}".format(s[:width], placeholder)


class ListProxyItem(Item):
    """Item for entries inheriting from Sdf ListProxy types.

    These are:
    - Sdf.PrimSpec.variantSetNameList
    - Sdf.PrimSpec.referenceList
    - Sdf.PrimSpec.payloadList

    """
    def __init__(self, proxy, value, data):
        super(ListProxyItem, self).__init__(data)
        self._list_proxy = proxy
        self._list_value = value

    def delete(self):
        self._list_proxy.remove(self._list_value)


class MapProxyItem(Item):
    """Item for entries inheriting from Sdf.MapEditProxy.

    These are:
    - Sdf.PrimSpec.variantSets
    - Sdf.PrimSpec.variantSelections
    - Sdf.PrimSpec.relocates

    """
    def __init__(self, proxy, key, data):
        super(MapProxyItem, self).__init__(data)
        self._key = key
        self._proxy = proxy

    def delete(self):
        # Delete the key from the parent proxy view
        del self._proxy[self._key]


class SpecInfoItem(Item):
    """Item for SceneSpecInfo entries."""
    def delete(self):
        # Delete the info from the parent spec
        parent = self.parent()
        spec = parent.get("spec")
        if not spec:
            return

        self.parent()["spec"].ClearInfo(self["name"])

    def move(self, target_layer):
        parent = self.parent()
        spec = parent.get("spec")
        if not spec:
            return

        layer = spec.layer
        if layer == target_layer:
            return

        path = parent["path"]
        prim_spec = Sdf.CreatePrimInLayer(target_layer, path)
        prim_spec.SetInfo(self["name"], self["default"])
        self.delete()


class SpecifierDelegate(QtWidgets.QStyledItemDelegate):
    """Delegate for "specifier" key to allow editing via combobox"""
    _VALUES = [[s.displayName, s] for s in Sdf.Specifier.allValues]

    def createEditor(self, parent, option, index):
        editor = QtWidgets.QComboBox(parent)
        for label, value in self._VALUES:
            editor.addItem(label, value)
        editor.currentIndexChanged.connect(self.onIndexChanged)
        editor.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        return editor

    def setEditorData(self, editor, index):
        value = index.data(QtCore.Qt.EditRole)
        editor.setCurrentText(value.displayName)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData(), QtCore.Qt.EditRole)

    @QtCore.Slot()
    def onIndexChanged(self):
        self.commitData.emit(self.sender())


class StageSdfModel(TreeModel):
    """Model listing a Stage's Layers and PrimSpecs"""
    # TODO: Add support for
    #   - "variantSelections",
    #   - "variantSetNameList",
    #   - "variantSets",
    #   - "relocates"
    Columns = ["name", "specifier", "typeName", "default", "type"]
    Colors = {
        "Layer": QtGui.QColor("#008EC5"),
        "PseudoRootSpec": QtGui.QColor("#A2D2EF"),
        "PrimSpec": QtGui.QColor("#A2D2EF"),
        "RelationshipSpec": QtGui.QColor("#FCD057"),
        "AttributeSpec": QtGui.QColor("#FFC8DD"),
        "reference": QtGui.QColor("#C8DDDD"),
        "payload": QtGui.QColor("#DDC8DD"),
        "VariantSetSpec": QtGui.QColor("#AAE48B"),
        "VariantSpec":  QtGui.QColor("#D6E8CC"),
        "variantSetName":  QtGui.QColor("#D6E8CC"),
        "variantSelections":  QtGui.QColor("#D6E8CC"),
        "SceneSpecInfo":  QtGui.QColor("#D6E8CC"),
    }

    def __init__(self, stage=None, parent=None):
        super(StageSdfModel, self).__init__(parent)
        self._stage = stage

        self._icon_provider = PrimTypeIconProvider()

    def setStage(self, stage):
        self._stage = stage

    @report_error
    def refresh(self):
        self.clear()

        stage = self._stage
        if not stage:
            return

        for layer in stage.GetLayerStack():

            layer_item = Item({
                "name": layer.GetDisplayName() or layer.identifier,
                "identifier": layer.identifier,
                "specifier": None,
                "type": layer.__class__.__name__
            })
            self.add_child(layer_item)

            items_by_path = {}

            def _traverse(path):
                spec = layer.GetObjectAtPath(path)
                if not spec:
                    # ignore target list binding entries or e.g. variantSetSpec
                    items_by_path[path] = Item({
                        "name": path.elementString,
                        "path": path,
                        "type": path.__class__.__name__
                    })
                    return

                icon = None
                spec_item = Item({
                    "name": spec.name,
                    "spec": spec,
                    "path": path,
                    "type": spec.__class__.__name__
                })

                element_string = spec.path.elementString
                if element_string and spec.name != element_string:
                    spec_item["name"] = element_string

                if hasattr(spec, "GetTypeName"):
                    spec_type_name = spec.GetTypeName()
                    icon = self._icon_provider.get_icon_from_type_name(
                        spec_type_name)
                    if icon:
                        spec_item["icon"] = icon

                if isinstance(spec, Sdf.PrimSpec):
                    if not icon:
                        # If the current layer doesn't specify a type, e.g.
                        # it is an "Over" but another layer does specify
                        # a type, then use that type instead
                        prim = stage.GetPrimAtPath(path)
                        if prim:
                            icon = self._icon_provider.get_icon(prim)
                            if icon:
                                spec_item["icon"] = icon

                    spec_item["specifier"] = spec.specifier
                    type_name = spec.typeName
                    spec_item["typeName"] = type_name

                    def _add_map_item(attr):
                        """Add MapProxyItem for list attribute on Spec"""
                        proxy = getattr(spec, attr)

                        # `prim_spec.variantSelections.keys()` can fail
                        # todo: figure out why this workaround is needed
                        try:
                            keys = list(proxy.keys())
                        except RuntimeError:
                            return

                        for key in keys:
                            proxy_item = MapProxyItem(
                                key=key,
                                proxy=proxy,
                                data={
                                    "name": key,
                                    "default": proxy.get(key),  # value
                                    "type": attr,
                                    "typeName": attr,
                                }
                            )
                            spec_item.add_child(proxy_item)

                    def _add_list_item(attr):
                        """Add ListProxyItem for list attribute on Spec"""
                        list_changes = getattr(spec, attr + "List")
                        for change_type in LIST_ATTRS:
                            changes_for_type = getattr(list_changes,
                                                       change_type)
                            for change in changes_for_type:

                                if hasattr(change, "assetPath"):
                                    # Sdf.Reference and Sdf.Payload
                                    name = change.assetPath
                                else:
                                    # variantSetName
                                    name = str(change)

                                list_change_item = ListProxyItem(
                                    proxy=changes_for_type,
                                    value=change,
                                    data={
                                        "name": name,
                                        # Strip off "Items"
                                        "default": change_type[:-5],
                                        "type": attr,
                                        "typeName": attr,
                                        "parent": changes_for_type
                                    }
                                )
                                spec_item.add_child(list_change_item)
                        if list_changes:
                            spec_item[attr] = str(list_changes)

                    # Add these types intermixed just so we order attributes
                    # together nicely that are somewhat related, e.g. variant
                    # information together
                    for attr, add_fn in [
                        ("reference", _add_list_item),
                        ("payload", _add_list_item),
                        ("relocates", _add_map_item),
                        ("variantSelections", _add_map_item),
                        ("variantSetName", _add_list_item),
                    ]:
                        add_fn(attr)
                    if hasattr(spec, "ListInfoKeys"):
                        for info in spec.ListInfoKeys():
                            if info in ["specifier", "references", "payloads", "variantSelection"]:
                                continue
                            try:
                                info_item = SpecInfoItem({
                                    "name": info,
                                    "default": spec.GetInfo(info),
                                    "type": "SceneSpecInfo"
                                })
                            except Exception:
                                log.warning("Failed to add SpecInfoItem (%s, %s)", info, spec)
                                continue
                            spec_item.add_child(info_item)

                elif isinstance(spec, Sdf.AttributeSpec):
                    value = spec.default
                    spec_item["default"] = shorten(str(value), 60)

                    type_name = spec.roleName
                    if not type_name and value is not None:
                        type_name = type(value).__name__
                    spec_item["typeName"] = type_name

                items_by_path[path] = spec_item

            layer.Traverse("/", _traverse)

            # Build hierarchy of item of specs
            for path, item in sorted(items_by_path.items()):
                parent = path.GetParentPath()
                parent_item = items_by_path.get(parent, layer_item)
                parent_item.add_child(item)

    def flags(self, index):

        if index.column() == 1:  # specifier
            item = index.internalPointer()
            spec = item.get("spec")
            # Match only exact PrimSpec type; we do not want PseudoRootSpec
            if spec and type(spec) is Sdf.PrimSpec:
                return (
                    QtCore.Qt.ItemIsEnabled |
                    QtCore.Qt.ItemIsSelectable |
                    QtCore.Qt.ItemIsEditable
                )

        return super(StageSdfModel, self).flags(index)

    def setData(self, index, value, role) -> bool:

        if index.column() == 1:  # specifier
            item = index.internalPointer()
            spec = item.get("spec")
            if spec and isinstance(spec, Sdf.PrimSpec):
                spec.specifier = value
                return True

        return super(StageSdfModel, self).setData(index, value, role)

    def data(self, index, role):

        if role == QtCore.Qt.ForegroundRole:
            item = index.data(TreeModel.ItemRole)
            class_type_name = item.get("type")
            color = self.Colors.get(class_type_name)
            return color

        if index.column() == 1 and role == QtCore.Qt.DisplayRole:
            item = index.data(TreeModel.ItemRole)
            specifier = item.get("specifier")
            if specifier:
                return specifier.displayName

        if index.column() == 2 and role == QtCore.Qt.DecorationRole:
            item = index.data(TreeModel.ItemRole)
            return item.get("icon")

        if role == QtCore.Qt.ToolTipRole:
            item = index.data(TreeModel.ItemRole)
            path = item.get("path")
            if path and isinstance(path, Sdf.Path):
                path = path.pathString
            return path

        return super(StageSdfModel, self).data(index, role)


class PrimSpectTypeFilterProxy(QtCore.QSortFilterProxyModel):

    def __init__(self, *args, **kwargs):
        super(PrimSpectTypeFilterProxy, self).__init__(*args, **kwargs)
        self._filter_types = set()

    def set_types_filter(self, types):
        self._filter_types = set(types)
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        index = model.index(source_row, 0, source_parent)
        if not index.isValid():
            return False

        item = index.data(TreeModel.ItemRole)
        item_type = item.get("type")
        if (
                self._filter_types
                and item_type
                and item_type not in self._filter_types
        ):
            return False

        return super(PrimSpectTypeFilterProxy,
                     self).filterAcceptsRow(source_row, source_parent)


class FilterListWidget(QtWidgets.QListWidget):
    def __init__(self, parent=None):
        super(FilterListWidget, self).__init__(parent)

        # Some labels are indented just to easily visually group some related
        # options together
        labels = [
            "Layer",
            # This is hidden since it's usually not filtered to
            # "PseudoRootSpec",
            "PrimSpec",
            "    reference",
            "    payload",
            "    relocates",
            "AttributeSpec",
            "RelationshipSpec",
            "SceneSpecInfo",
            "VariantSetSpec",
            "    VariantSpec",
            "    variantSetName",
            "    variantSelections",
        ]
        tooltips = {
            "Layer": "A single <b>Layer</b> in the stage.",
            "PrimSpec": "A <b>Prim</b> description",
            "reference": "Represents a reference.",
            "payload": (
                "Represents a payload.<br>"
                "Payloads are similar to prim references with the major "
                "difference that payloads are explicitly loaded by the user."
            ),
            "relocates": "Namespace relocations specified on a prim",
            "AttributeSpec": "A property that holds typed data.",
            "RelationshipSpec": (
                "A property that contains a reference to "
                "one or more prim specs."
            ),
            "SceneSpecInfo": "Metadata attached to the parent spec.",
            "VariantSetSpec": "A variant set.",
            "VariantSpec": "A single variant opinion for a variant set.",
            "variantSetName": "Defines available variant set name on a prim.",
            "variantSelections": (
                "Defines the selected variant for a variant set."
            )
        }

        for label in labels:
            type_name = label.lstrip(" ")
            item = QtWidgets.QListWidgetItem(label, self)

            # Set color
            item.setData(QtCore.Qt.ForegroundRole,
                         StageSdfModel.Colors.get(type_name))

            tooltip = tooltips.get(type_name)
            if tooltip:
                item.setData(QtCore.Qt.ToolTipRole, tooltip)

        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)


class SpecEditorWindow(QtWidgets.QDialog):
    def __init__(self, stage, parent=None):
        super(SpecEditorWindow, self).__init__(parent=parent)

        self.setWindowTitle("USD Layer Spec Editor")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QtWidgets.QSplitter()

        filter_list = FilterListWidget(self)
        filter_list.itemSelectionChanged.connect(
            self._on_filter_selection_changed
        )

        editor = SpecEditsWidget(stage, self)

        splitter.addWidget(filter_list)
        splitter.addWidget(editor)
        splitter.setSizes([100, 700])
        layout.addWidget(splitter)

        self.editor = editor
        self.filter_list = filter_list

    def _on_filter_selection_changed(self):
        items = self.filter_list.selectedItems()
        types = {item.text().strip() for item in items}
        self.editor.proxy.set_types_filter(types)
        self.editor.view.expandAll()


class SpecEditsWidget(QtWidgets.QWidget):
    def __init__(self, stage=None, parent=None):
        super(SpecEditsWidget, self).__init__(parent=parent)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        filter_edit = QtWidgets.QLineEdit()
        filter_edit.setPlaceholderText("Filter")

        model = StageSdfModel(stage)
        proxy = PrimSpectTypeFilterProxy()
        proxy.setRecursiveFilteringEnabled(True)
        proxy.setSourceModel(model)
        view = QtWidgets.QTreeView()
        view.setModel(proxy)
        view.setIndentation(10)
        view.setIconSize(QtCore.QSize(20, 20))
        view.setStyleSheet(
            "QTreeView::item {"
            "   height: 20px;"
            "   padding: 1px 5px 1px 5px;"
            "   margin: 0px;"
            "}"
        )
        specifier_delegate = SpecifierDelegate(self)
        view.setItemDelegateForColumn(1, specifier_delegate)
        view.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        view.setUniformRowHeights(True)
        view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        view.customContextMenuRequested.connect(self.on_context_menu)

        auto_refresh = QtWidgets.QCheckBox("Auto Refresh on Stage Changes")
        auto_refresh.setChecked(True)
        refresh = QtWidgets.QPushButton("Refresh")
        delete = QtWidgets.QPushButton("Delete")

        layout.addWidget(filter_edit)
        layout.addWidget(view)
        layout.addWidget(auto_refresh)
        layout.addWidget(refresh)
        layout.addWidget(delete)

        self.filter_edit = QtWidgets
        self.auto_refresh = auto_refresh
        self.model = model
        self.proxy = proxy
        self.view = view
        self._specifier_delegate = specifier_delegate

        auto_refresh.stateChanged.connect(self.set_refresh_on_changes)
        refresh.clicked.connect(self.on_refresh)
        delete.clicked.connect(self.on_delete)
        filter_edit.textChanged.connect(self.on_filter_changed)

        self._listeners = []

        self.set_refresh_on_changes(True)
        self.on_refresh()

    def set_refresh_on_changes(self, state):
        if state:
            if self._listeners:
                return
            log.debug("Adding Prim Spec listener")
            sender = self.model._stage
            listener = Tf.Notice.Register(Usd.Notice.StageContentsChanged,
                                          self.on_stage_changed_notice,
                                          sender)
            self._listeners.append(listener)
        else:
            if not self._listeners:
                return
            log.debug("Removing Prim Spec listeners")
            for listener in self._listeners:
                listener.Revoke()
            self._listeners.clear()

    def on_stage_changed_notice(self, notice, sender):
        self.proxy.invalidate()
        schedule(self.on_refresh, 100, channel="changes")

    def on_filter_changed(self, text):
        self.proxy.setFilterRegularExpression(".*{}.*".format(text))
        self.proxy.invalidateFilter()
        self.view.expandAll()

    def showEvent(self, event):
        state = self.auto_refresh.checkState() == QtCore.Qt.Checked
        self.set_refresh_on_changes(state)

    def hideEvent(self, event):
        # Remove any callbacks if they exist
        self.set_refresh_on_changes(False)

    def on_context_menu(self, point):

        menu = QtWidgets.QMenu(self.view)

        action = menu.addAction("Delete")
        action.triggered.connect(self.on_delete)

        move_menu = menu.addMenu("Move to layer")

        stage = self.model._stage
        for layer in stage.GetLayerStack():
            label = layer.GetDisplayName() or layer.identifier
            action = move_menu.addAction(label)
            action.setData(layer)

        def move_to(action):
            layer = action.data()
            self.move_selection_to_layer(layer)

        move_menu.triggered.connect(move_to)

        menu.exec_(self.view.mapToGlobal(point))

    def on_refresh(self):
        self.model.refresh()
        self.proxy.invalidate()
        self.view.resizeColumnToContents(0)
        self.view.expandAll()
        self.view.resizeColumnToContents(1)
        self.view.resizeColumnToContents(2)
        self.view.resizeColumnToContents(3)
        self.view.resizeColumnToContents(4)

    def delete_indexes(self, indexes):
        specs = []
        deletables = []
        for index in indexes:
            item = index.data(TreeModel.ItemRole)
            spec = item.get("spec")
            if item.get("type") == "PseudoRootSpec":
                continue

            if spec:
                specs.append(spec)
            elif hasattr(item, "delete"):
                # MapProxyItem and ListProxyItem entries
                deletables.append(item)

        if not specs and not deletables:
            return False

        with Sdf.ChangeBlock():
            for spec in specs:
                if spec and not spec.expired:
                    log.debug(f"Removing spec: %s", spec.path)
                    remove_spec(spec)
            for deletable in deletables:
                deletable.delete()
            stage = self.model._stage
            for layer in stage.GetLayerStack():
                layer.RemoveInertSceneDescription()
        return True

    def on_delete(self):
        selection_model = self.view.selectionModel()
        rows = selection_model.selectedRows()
        has_deleted = self.delete_indexes(rows)
        if has_deleted and not self._listeners:
            self.on_refresh()

    def move_selection_to_layer(self, target_layer):
        """Move Sdf.Spec to another Sdf.Layer

        Note: If moved to a PrimSpec path already existing in the target layer
        then any opinions on that PrimSpec or it children are removed. It
        replaces the prim spec. It does not merge into an existing PrimSpec.

        """

        selection_model = self.view.selectionModel()
        rows = selection_model.selectedRows()

        specs = []
        for index in rows:
            item = index.data(TreeModel.ItemRole)
            if item.get("type") == "PseudoRootSpec":
                continue
            elif item.get("type") == "SceneSpecInfo":
                item.move(target_layer)
                continue

            spec = item.get("spec")
            if spec:
                specs.append(spec)

        # Get highest paths in the spec selection and exclude any selected
        # children since those will be moved along anyway
        paths = {spec.path.pathString for spec in specs}
        top_specs = []
        for spec in specs:

            skip = False
            parent_path = spec.path.pathString.rsplit("/", 1)[0]
            while "/" in parent_path:
                if parent_path in paths:
                    skip = True
                    break
                parent_path = parent_path.rsplit("/", 1)[0]
            if skip:
                continue

            top_specs.append(spec)

        if not top_specs:
            return

        # Now we need to create specs up to each top spec's path in the
        # target layer if these do not exist yet.
        for spec in top_specs:
            src_layer = spec.layer

            prim_path = spec.path.GetPrimPath()
            if not target_layer.GetPrimAtPath(prim_path):
                Sdf.CreatePrimInLayer(target_layer, prim_path)
            copy_spec_merge(src_layer, spec.path, target_layer, spec.path)

        # Delete the specs on the original layer
        for spec in top_specs:
            remove_spec(spec)

        if not self._listeners:
            self.on_refresh()
