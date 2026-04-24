
"""
打包脚本 - 将柏林1945游戏打包为Windows可执行文件
使用方法：python build_exe.py
"""

import os
import sys
import subprocess
import shutil

def main():
    # 获取当前脚本所在目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    game_script = os.path.join(script_dir, "柏林1945_虎王车长_系统版.py")
    
    # 检查游戏脚本是否存在
    if not os.path.exists(game_script):
        print(f"错误：找不到游戏脚本 {game_script}")
        return False
    
    print("=" * 50)
    print("柏林1945 - 打包工具")
    print("=" * 50)
    print(f"游戏脚本: {game_script}")
    print()
    
    # 检查PyInstaller是否已安装
    try:
        import PyInstaller
        print(f"✓ PyInstaller已安装 (版本: {PyInstaller.__version__})")
    except ImportError:
        print("✗ PyInstaller未安装")
        print("正在安装PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
        print("✓ PyInstaller安装完成")
    
    print()
    print("开始打包...")
    print("-" * 50)
    
    # PyInstaller打包命令
    # --onefile: 打包成单个exe文件
    # --windowed: 不显示控制台窗口（注释掉，因为我们这是命令行游戏，需要控制台）
    # --name: 指定输出文件名
    # --icon: 可以指定图标文件（如果有的话）
    # --add-data: 如果需要包含额外文件
    
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",           # 打包成单个exe
        "--name=柏林1945_虎王车长_系统版",   # 输出文件名
        "--console",           # 显示控制台窗口（命令行游戏需要）
        "--clean",             # 清理临时文件
        game_script
    ]
    
    try:
        subprocess.check_call(cmd)
        print()
        print("=" * 50)
        print("✓ 打包完成！")
        print("=" * 50)
        print(f"可执行文件位置: {os.path.join(script_dir, 'dist', '柏林1945_虎王车长_系统版.exe')}")
        print()
        print("提示：")
        print("1. 打包后的exe文件位于 dist 文件夹中")
        print("2. 你可以直接运行该exe文件，无需安装Python")
        print("3. 可以将exe文件复制到任何Windows电脑上运行")
        return True
    except subprocess.CalledProcessError as e:
        print()
        print("=" * 50)
        print("✗ 打包失败")
        print("=" * 50)
        print(f"错误信息: {e}")
        return False
    except Exception as e:
        print()
        print("=" * 50)
        print("✗ 打包过程出错")
        print("=" * 50)
        print(f"错误信息: {e}")
        return False

if __name__ == "__main__":
    success = main()
    input("\n按Enter键退出...")
    sys.exit(0 if success else 1)

