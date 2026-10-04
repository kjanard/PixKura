import re
import random
from PyQt6.QtCore import Qt, QAbstractListModel, QModelIndex
from PyQt6.QtGui import QIcon

class ThumbnailListModel(QAbstractListModel):
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        self._data = data or []  
        # O(1) lookup map from item_id -> row_index
        self._id_map = {item['id']: idx for idx, item in enumerate(self._data) if isinstance(item, dict) and 'id' in item}
        self.sort_mode = 0  

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return None
        row = index.row()
        if row < 0 or row >= len(self._data): return None

        item = self._data[row]
        
        if role == Qt.ItemDataRole.DisplayRole:
            if item.get('type') == 'FOLDER':
                return f"{item.get('name')} ({item.get('count', 0)})"
            return item.get('name')
        elif role == Qt.ItemDataRole.DecorationRole:
            return item.get('icon')
        elif role == Qt.ItemDataRole.UserRole:
            return item.get('id')
        elif role == Qt.ItemDataRole.UserRole + 1:
            return item.get('type')
        elif role == Qt.ItemDataRole.UserRole + 2:
            return item.get('count', 0)
        elif role == Qt.ItemDataRole.UserRole + 3:
            return item.get('mtime', 0.0)
        elif role == Qt.ItemDataRole.UserRole + 4:
            return item.get('ctime', 0.0)
        elif role == Qt.ItemDataRole.ToolTipRole:
            name = item.get('name', '')
            if item.get('type') == 'FOLDER':
                return f"📁 {name}\n({item.get('count', 0)} files)"
            
            # Check cached tooltip or fetch dynamically from DB
            if 'tags_info' not in item:
                path = item.get('id', '')
                try:
                    from database import get_file_tags
                    from config import DB_FILE
                    tags = get_file_tags(DB_FILE, path)
                    if tags and tags.get('all'):
                        lines = [f"📄 {name}"]
                        if tags.get('characters'):
                            chars_str = ", ".join([f"{c[0].replace('_', ' ').title()} ({int(c[1]*100)}%)" for c in tags['characters']])
                            lines.append(f"👤 Character: {chars_str}")
                        if tags.get('series'):
                            series_str = ", ".join([f"{s[0].replace('_', ' ').title()}" for s in tags['series']])
                            lines.append(f"📚 Series: {series_str}")
                        if tags.get('general'):
                            gen_tags = ", ".join([g[0] for g in tags['general'][:10]])
                            lines.append(f"🏷️ Tags: {gen_tags}")
                        if tags.get('rating'):
                            lines.append(f"🔞 Rating: {tags['rating'].title()}")
                        item['tags_info'] = "\n".join(lines)
                    else:
                        item['tags_info'] = f"📄 {name}\n(ℹ️ No AI tags scanned yet)"
                except Exception:
                    item['tags_info'] = name

            return item.get('tags_info', name)

            
        return None

    def natural_keys(self, text):
        if not text: return []
        # Return list of tuples: (0, int) for numbers, (1, str) for text to ensure 100% type-safe comparison
        return [(0, int(c)) if c.isdigit() else (1, c.lower()) for c in re.split(r'(\d+)', str(text)) if c]

    def get_row_by_id(self, item_id):
        return self._id_map.get(item_id, -1)

    def sort(self, mode):
        self.sort_mode = mode
        self.layoutAboutToBeChanged.emit()
        
        if mode == 8: # random
            random.shuffle(self._data)
        elif mode == 0: # Name (A-Z)
            self._data.sort(key=lambda item: self.natural_keys(item.get('name', '')))
        elif mode == 1: # Name (Z-A)
            self._data.sort(key=lambda item: self.natural_keys(item.get('name', '')), reverse=True)
        elif mode == 2: # Count/Size (High->Low)
            self._data.sort(key=lambda item: (-(item.get('count') or 0), self.natural_keys(item.get('name', ''))))
        elif mode == 3: # Count/Size (Low->High)
            self._data.sort(key=lambda item: ((item.get('count') or 0), self.natural_keys(item.get('name', ''))))
        elif mode == 4: # Date Modified (New->Old)
            self._data.sort(key=lambda item: (-(item.get('mtime') or 0.0), self.natural_keys(item.get('name', ''))))
        elif mode == 5: # Date Modified (Old->New)
            self._data.sort(key=lambda item: ((item.get('mtime') or 0.0), self.natural_keys(item.get('name', ''))))
        elif mode == 6: # Date Created (New->Old)
            self._data.sort(key=lambda item: (-(item.get('ctime') or 0.0), self.natural_keys(item.get('name', ''))))
        elif mode == 7: # Date Created (Old->New)
            self._data.sort(key=lambda item: ((item.get('ctime') or 0.0), self.natural_keys(item.get('name', ''))))
                
        # Rebuild O(1) ID map after sorting
        self._id_map = {item['id']: idx for idx, item in enumerate(self._data) if isinstance(item, dict) and 'id' in item}
        self.layoutChanged.emit()

    def update_data(self, new_data):
        self.beginResetModel()
        self._data = new_data
        self._id_map = {item['id']: idx for idx, item in enumerate(self._data) if isinstance(item, dict) and 'id' in item}
        self.endResetModel()

    def append_items(self, new_items):
        if not new_items: return
        start_idx = len(self._data)
        self.beginInsertRows(QModelIndex(), start_idx, start_idx + len(new_items) - 1)
        for idx, item in enumerate(new_items, start=start_idx):
            if isinstance(item, dict) and 'id' in item:
                self._id_map[item['id']] = idx
        self._data.extend(new_items)
        self.endInsertRows()

    def update_icon(self, index_row, new_icon):
        if 0 <= index_row < len(self._data):
            self._data[index_row]['icon'] = new_icon
            idx = self.index(index_row, 0)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def get_item_data(self, index_row):
        if 0 <= index_row < len(self._data):
            return self._data[index_row]
        return None
